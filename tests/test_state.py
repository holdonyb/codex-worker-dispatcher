from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import errno
import json
import os
import stat
import subprocess
import sys
import tempfile
from threading import Barrier
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import call, patch

import codex_worker_dispatcher.state as state_module
from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.process import windows_no_window_flags
from codex_worker_dispatcher.state import (
    StateStore,
    TERMINAL_STATES,
    default_state_root,
    utc_now,
    valid_task_id,
)


def _handle_symlink_setup_error(
    test_case: unittest.TestCase,
    error: OSError,
) -> None:
    if (
        isinstance(error, PermissionError)
        or error.errno in {errno.EACCES, errno.EPERM}
        or getattr(error, "winerror", None) == 1314
    ):
        test_case.skipTest(f"Directory symlinks unavailable: {error}")
        return
    raise error


def _close_capability_for_test(capability: object) -> None:
    close = getattr(capability, "close", None)
    if close is not None:
        close()
        return
    capability.__exit__(None, None, None)  # type: ignore[attr-defined]


class StateContractTests(unittest.TestCase):
    def test_terminal_states_are_exact(self) -> None:
        self.assertEqual(
            TERMINAL_STATES,
            {"completed", "failed", "timed_out", "cancelled", "reaped", "orphaned"},
        )

    def test_utc_now_is_timezone_aware_utc_iso_8601(self) -> None:
        parsed = datetime.fromisoformat(utc_now())

        self.assertIsNotNone(parsed.tzinfo)
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(parsed))

    def test_task_id_validation_uses_the_public_ascii_contract(self) -> None:
        for task_id in (
            "a",
            "a0",
            "20260717-120000-a1b2c3d4",
            "task.name_with-mixed.parts",
            "com0",
            "com10.txt",
            "con-task",
            "a" * 128,
        ):
            with self.subTest(task_id=task_id):
                self.assertTrue(valid_task_id(task_id))

        for task_id in (
            "",
            "A0",
            "Task",
            "../task",
            "task/child",
            r"task\child",
            "-leading",
            ".leading",
            "has space",
            "has\twhitespace",
            "task.",
            "CON",
            "con",
            "nul.txt",
            "prn.log",
            "aux",
            "com1",
            "com9.txt",
            "lpt1",
            "lpt9.log",
            "a" * 129,
        ):
            with self.subTest(task_id=task_id):
                self.assertFalse(valid_task_id(task_id))

    def test_default_state_root_prefers_nonempty_codex_home(self) -> None:
        configured_home = Path("configured") / "codex-home"

        with patch.dict(os.environ, {"CODEX_HOME": str(configured_home)}):
            self.assertEqual(
                default_state_root(),
                (configured_home / "worker-runs").expanduser().resolve(strict=False),
            )

    def test_default_state_root_falls_back_to_patched_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory) / "home"
            for codex_home in ("", None):
                with self.subTest(codex_home=codex_home):
                    environment = {} if codex_home is None else {"CODEX_HOME": ""}
                    with patch.dict(os.environ, environment, clear=True), patch.object(
                        Path, "home", return_value=home
                    ):
                        self.assertEqual(
                            default_state_root(),
                            (home / ".codex" / "worker-runs").resolve(strict=False),
                        )


class StateStorePathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = (
            Path(self.temporary_directory.name).resolve(strict=False)
            / "state"
            / "worker-runs"
        )

    def test_constructor_is_lazy_and_task_dir_maps_valid_id(self) -> None:
        store = StateStore(self.root)

        self.assertFalse(self.root.exists())
        self.assertEqual(
            store.task_dir("20260717-120000-a1b2c3d4"),
            (self.root / "20260717-120000-a1b2c3d4").resolve(strict=False),
        )
        self.assertFalse(self.root.exists())

    def test_default_constructor_uses_default_root_without_creating_it(self) -> None:
        expected_root = Path(self.temporary_directory.name) / "default" / "worker-runs"

        with patch(
            "codex_worker_dispatcher.state.default_state_root",
            return_value=expected_root,
        ):
            store = StateStore()

        self.assertEqual(store.root, expected_root.resolve(strict=False))
        self.assertFalse(expected_root.exists())

        store.create_task_dir("created-later")
        self.assertTrue(expected_root.is_dir())

    def test_unexpected_symlink_setup_error_is_reraised(self) -> None:
        unexpected = OSError(errno.EIO, "unexpected symlink failure")

        with self.assertRaises(OSError) as raised:
            _handle_symlink_setup_error(self, unexpected)

        self.assertIs(raised.exception, unexpected)

    def test_task_dir_rejects_invalid_ids_without_creating_root(self) -> None:
        store = StateStore(self.root)

        for task_id in (
            "",
            "A0",
            "Task",
            "../task",
            "task/child",
            r"task\child",
            "-leading",
            ".leading",
            "has space",
            "has\twhitespace",
            "task.",
            "CON",
            "con",
            "nul.txt",
            "com1.log",
            "lpt9",
            "a" * 129,
        ):
            with self.subTest(task_id=task_id):
                for operation in (store.task_dir, store.create_task_dir):
                    with self.subTest(operation=operation):
                        with self.assertRaises(WorkerError) as raised:
                            operation(task_id)
                        self.assertEqual(raised.exception.code, "invalid_task_id")
        self.assertFalse(self.root.exists())

    def test_create_task_dir_creates_private_directory(self) -> None:
        store = StateStore(self.root)

        task_dir = store.create_task_dir("20260717-120000-a1b2c3d4")

        self.assertEqual(task_dir, self.root / "20260717-120000-a1b2c3d4")
        self.assertTrue(task_dir.is_dir())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(task_dir.stat().st_mode), 0o700)

    def test_task_directory_creation_fsyncs_root_after_mkdir(self) -> None:
        self.root.mkdir(parents=True)
        store = StateStore(self.root)
        real_mkdir = os.mkdir
        events: list[tuple[str, str]] = []

        def recording_mkdir(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            events.append(("mkdir", Path(os.fsdecode(path)).name))
            if dir_fd is None:
                real_mkdir(path, mode)
            else:
                real_mkdir(path, mode, dir_fd=dir_fd)

        def recording_fsync(path: Path, file_descriptor: int | None = None) -> None:
            events.append(("fsync", path.name))

        with patch.object(
            state_module.os, "mkdir", side_effect=recording_mkdir
        ), patch.object(
            state_module, "_fsync_directory", side_effect=recording_fsync
        ):
            store.create_task_dir("durable-task")

        self.assertEqual(
            events,
            [("mkdir", "durable-task"), ("fsync", self.root.name)],
        )

    def test_lazy_root_creation_fsyncs_parent_before_task_creation(self) -> None:
        self.root.parent.mkdir(parents=True)
        store = StateStore(self.root)
        real_mkdir = os.mkdir
        events: list[tuple[str, str]] = []

        def recording_mkdir(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            events.append(("mkdir", Path(os.fsdecode(path)).name))
            if dir_fd is None:
                real_mkdir(path, mode)
            else:
                real_mkdir(path, mode, dir_fd=dir_fd)

        def recording_fsync(path: Path, file_descriptor: int | None = None) -> None:
            events.append(("fsync", path.name))

        with patch.object(
            state_module.os, "mkdir", side_effect=recording_mkdir
        ), patch.object(
            state_module, "_fsync_directory", side_effect=recording_fsync
        ):
            store.create_task_dir("durable-task")

        self.assertEqual(
            events,
            [
                ("mkdir", self.root.name),
                ("fsync", self.root.parent.name),
                ("mkdir", "durable-task"),
                ("fsync", self.root.name),
            ],
        )

    def test_missing_ancestor_creation_fsyncs_each_parent_in_order(self) -> None:
        base = Path(self.temporary_directory.name).resolve(strict=False)
        root = base / "missing-a" / "missing-b" / "state"
        store = StateStore(root)
        real_mkdir = os.mkdir
        events: list[tuple[str, str]] = []

        def recording_mkdir(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            if dir_fd is None:
                real_mkdir(path, mode)
            else:
                real_mkdir(path, mode, dir_fd=dir_fd)
            events.append(("mkdir", Path(os.fsdecode(path)).name))

        def recording_fsync(path: Path, file_descriptor: int | None = None) -> None:
            events.append(("fsync", path.name))

        with patch.object(
            state_module.os, "mkdir", side_effect=recording_mkdir
        ), patch.object(
            state_module, "_fsync_directory", side_effect=recording_fsync
        ):
            store.create_task_dir("durable-task")

        self.assertEqual(
            events,
            [
                ("mkdir", "missing-a"),
                ("fsync", base.name),
                ("mkdir", "missing-b"),
                ("fsync", "missing-a"),
                ("mkdir", "state"),
                ("fsync", "missing-b"),
                ("mkdir", "durable-task"),
                ("fsync", "state"),
            ],
        )

    def test_concurrent_ancestor_and_root_creation_fsyncs_parents(self) -> None:
        base = Path(self.temporary_directory.name).resolve(strict=False)
        root = base / "missing-a" / "missing-b" / "state"
        store = StateStore(root)
        real_mkdir = os.mkdir
        events: list[tuple[str, str]] = []
        raced_names = {"missing-a", "state"}

        def racing_mkdir(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            name = Path(os.fsdecode(path)).name
            if dir_fd is None:
                real_mkdir(path, mode)
            else:
                real_mkdir(path, mode, dir_fd=dir_fd)
            if name in raced_names:
                events.append(("concurrent-mkdir", name))
                raise FileExistsError(errno.EEXIST, "created concurrently")
            events.append(("mkdir", name))

        def recording_fsync(path: Path, file_descriptor: int | None = None) -> None:
            events.append(("fsync", path.name))

        with patch.object(
            state_module.os, "mkdir", side_effect=racing_mkdir
        ), patch.object(
            state_module, "_fsync_directory", side_effect=recording_fsync
        ):
            store.create_task_dir("durable-task")

        self.assertEqual(
            events,
            [
                ("concurrent-mkdir", "missing-a"),
                ("fsync", base.name),
                ("mkdir", "missing-b"),
                ("fsync", "missing-a"),
                ("concurrent-mkdir", "state"),
                ("fsync", "missing-b"),
                ("mkdir", "durable-task"),
                ("fsync", "state"),
            ],
        )

    def test_concurrent_task_creation_fsyncs_root_before_task_exists(self) -> None:
        self.root.mkdir(parents=True)
        store = StateStore(self.root)
        real_mkdir = os.mkdir
        events: list[tuple[str, str]] = []

        def racing_mkdir(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> None:
            if dir_fd is None:
                real_mkdir(path, mode)
            else:
                real_mkdir(path, mode, dir_fd=dir_fd)
            events.append(("concurrent-mkdir", Path(os.fsdecode(path)).name))
            raise FileExistsError(errno.EEXIST, "created concurrently")

        def recording_fsync(path: Path, file_descriptor: int | None = None) -> None:
            events.append(("fsync", path.name))

        with patch.object(
            state_module.os, "mkdir", side_effect=racing_mkdir
        ), patch.object(
            state_module, "_fsync_directory", side_effect=recording_fsync
        ):
            with self.assertRaises(WorkerError) as raised:
                store.create_task_dir("concurrent-task")

        self.assertEqual(raised.exception.code, "task_exists")
        self.assertTrue((self.root / "concurrent-task").is_dir())
        self.assertEqual(
            events,
            [
                ("concurrent-mkdir", "concurrent-task"),
                ("fsync", self.root.name),
            ],
        )

    def test_create_task_dir_uses_stable_error_codes(self) -> None:
        store = StateStore(self.root)

        with self.assertRaises(WorkerError) as invalid:
            store.create_task_dir("../task")
        self.assertEqual(invalid.exception.code, "invalid_task_id")

        store.create_task_dir("existing")
        with self.assertRaises(WorkerError) as existing:
            store.create_task_dir("existing")
        self.assertEqual(existing.exception.code, "task_exists")

    def test_create_task_dir_reports_invalid_state_when_root_is_a_file(self) -> None:
        self.root.parent.mkdir(parents=True)
        self.root.write_text("not a directory", encoding="utf-8")
        store = StateStore(self.root)

        with self.assertRaises(WorkerError) as raised:
            store.create_task_dir("new-task")

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_create_task_dir_rejects_preexisting_root_symlink(self) -> None:
        external_root = Path(self.temporary_directory.name) / "external-root"
        external_root.mkdir()
        self.root.parent.mkdir(parents=True)
        try:
            self.root.symlink_to(external_root, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)
        store = StateStore(self.root)

        with self.assertRaises(WorkerError) as raised:
            store.create_task_dir("new-task")

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(list(external_root.iterdir()), [])

    def test_list_manifests_rejects_preexisting_root_symlink(self) -> None:
        external_root = Path(self.temporary_directory.name) / "external-root"
        external_root.mkdir()
        self.root.parent.mkdir(parents=True)
        try:
            self.root.symlink_to(external_root, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)
        store = StateStore(self.root)

        with self.assertRaises(WorkerError) as raised:
            store.list_manifests()

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_parent_alias_retarget_does_not_redirect_state_operations(self) -> None:
        base = Path(self.temporary_directory.name).resolve(strict=False)
        original_parent = base / "original-parent"
        external_parent = base / "external-parent"
        original_parent.mkdir()
        external_parent.mkdir()
        parent_alias = base / "parent-alias"
        try:
            parent_alias.symlink_to(original_parent, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)

        store = StateStore(parent_alias / "state")
        task = store.create_task_dir("stable-task")
        store.write_prompt(task, "original prompt")
        store.write_manifest(
            task,
            {"task_id": task.name, "status": "original"},
        )
        external_task = external_parent / "state" / task.name
        external_task.mkdir(parents=True)
        external_prompt = external_task / "prompt.txt"
        external_manifest = external_task / "manifest.json"
        external_prompt.write_text("external prompt", encoding="utf-8")
        external_manifest.write_text(
            json.dumps({"task_id": task.name, "status": "external"}),
            encoding="utf-8",
        )
        original_external_prompt = external_prompt.read_bytes()
        original_external_manifest = external_manifest.read_bytes()

        parent_alias.unlink()
        try:
            parent_alias.symlink_to(external_parent, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)

        prompt = store.read_prompt(task)
        manifest = store.read_manifest(task)
        manifests = store.list_manifests()
        store.write_prompt(task, "replacement prompt")
        store.write_manifest(
            task,
            {"task_id": task.name, "status": "replacement"},
        )
        created = store.create_task_dir("created-after-retarget")

        self.assertEqual(prompt, "original prompt")
        self.assertEqual(manifest["status"], "original")
        self.assertEqual(manifests[0]["status"], "original")
        self.assertEqual(
            (original_parent / "state" / task.name / "prompt.txt").read_text(
                encoding="utf-8"
            ),
            "replacement prompt",
        )
        self.assertEqual(
            json.loads(
                (
                    original_parent / "state" / task.name / "manifest.json"
                ).read_text(encoding="utf-8")
            )["status"],
            "replacement",
        )
        self.assertEqual(created.parent, original_parent / "state")
        self.assertEqual(external_prompt.read_bytes(), original_external_prompt)
        self.assertEqual(external_manifest.read_bytes(), original_external_manifest)
        self.assertFalse((external_parent / "state" / created.name).exists())

    def test_canonical_parent_swap_rejects_all_state_operations(self) -> None:
        base = Path(self.temporary_directory.name).resolve(strict=False)
        canonical_parent = base / "canonical-parent"
        external_parent = base / "external-parent"
        canonical_parent.mkdir()
        external_parent.mkdir()
        store = StateStore(canonical_parent / "state")
        task = store.create_task_dir("stable-task")
        store.write_prompt(task, "original prompt")
        store.write_manifest(
            task,
            {"task_id": task.name, "status": "original"},
        )
        external_task = external_parent / "state" / task.name
        external_task.mkdir(parents=True)
        external_prompt = external_task / "prompt.txt"
        external_manifest = external_task / "manifest.json"
        external_prompt.write_text("external prompt", encoding="utf-8")
        external_manifest.write_text(
            json.dumps({"task_id": task.name, "status": "external"}),
            encoding="utf-8",
        )
        original_external_prompt = external_prompt.read_bytes()
        original_external_manifest = external_manifest.read_bytes()

        moved_parent = base / "moved-canonical-parent"
        canonical_parent.replace(moved_parent)
        try:
            canonical_parent.symlink_to(external_parent, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)

        operations = (
            lambda: store.read_prompt(task),
            lambda: store.read_manifest(task),
            store.list_manifests,
            lambda: store.write_prompt(task, "replacement prompt"),
            lambda: store.write_manifest(
                task,
                {"task_id": task.name, "status": "replacement"},
            ),
            lambda: store.create_task_dir("created-after-parent-swap"),
        )
        errors: list[WorkerError] = []
        for operation in operations:
            try:
                operation()
            except WorkerError as error:
                errors.append(error)

        self.assertEqual(len(errors), len(operations))
        self.assertTrue(all(error.code == "invalid_state" for error in errors))
        self.assertEqual(external_prompt.read_bytes(), original_external_prompt)
        self.assertEqual(external_manifest.read_bytes(), original_external_manifest)
        self.assertFalse(
            (external_parent / "state" / "created-after-parent-swap").exists()
        )

    def test_create_task_dir_with_nonexistent_parent_path_still_works(self) -> None:
        root = (
            Path(self.temporary_directory.name).resolve(strict=False)
            / "missing-parent"
            / "nested"
            / "state"
        )
        store = StateStore(root)

        task = store.create_task_dir("created-task")

        self.assertEqual(task, root / "created-task")
        self.assertTrue(task.is_dir())

    def test_task_context_closes_task_and_root_descriptors(self) -> None:
        root = state_module._StateRoot(self.root, (1, 2, 3), 101)
        task = state_module._TaskDirectory(
            self.root / "task",
            (4, 5, 6),
            202,
            root,
        )

        with patch.object(state_module.os, "close") as close:
            with task:
                pass

        self.assertEqual(close.call_args_list, [call(202), call(101)])
        self.assertIsNone(task.file_descriptor)
        self.assertIsNone(task.root)
        self.assertIsNone(root.file_descriptor)

    def test_capability_close_is_idempotent_and_closes_owned_chain(self) -> None:
        parent = state_module._StateParent(self.root.parent, (1, 2, 3), 101)
        root = state_module._StateRoot(self.root, (4, 5, 6), 202, parent)
        task = state_module._TaskDirectory(
            self.root / "task",
            (7, 8, 9),
            303,
            root,
        )

        with patch.object(state_module.os, "close") as close:
            _close_capability_for_test(task)
            _close_capability_for_test(task)

        self.assertEqual(close.call_args_list, [call(303), call(202), call(101)])
        self.assertTrue(getattr(parent, "closed", False))
        self.assertTrue(getattr(root, "closed", False))
        self.assertTrue(getattr(task, "closed", False))

    def test_closed_capabilities_reject_context_and_validation(self) -> None:
        self.root.mkdir(parents=True)
        task_path = self.root / "task"
        task_path.mkdir()
        store = StateStore(self.root)

        parent = state_module._StateParent(
            self.root.parent,
            state_module._stat_identity(self.root.parent.lstat()),
        )
        root = state_module._StateRoot(
            self.root,
            state_module._stat_identity(self.root.lstat()),
            parent=state_module._StateParent(
                self.root.parent,
                state_module._stat_identity(self.root.parent.lstat()),
            ),
        )
        task = state_module._TaskDirectory(
            task_path,
            state_module._stat_identity(task_path.lstat()),
            root=state_module._StateRoot(
                self.root,
                state_module._stat_identity(self.root.lstat()),
                parent=state_module._StateParent(
                    self.root.parent,
                    state_module._stat_identity(self.root.parent.lstat()),
                ),
            ),
        )
        cases = (
            (parent, store._validate_parent),
            (root, store._validate_root),
            (task, store._validate_task_dir),
        )
        for capability, validate in cases:
            with self.subTest(capability=type(capability).__name__):
                _close_capability_for_test(capability)
                with self.assertRaises(WorkerError) as entered:
                    capability.__enter__()
                self.assertEqual(entered.exception.code, "invalid_state")
                with self.assertRaises(WorkerError) as validated:
                    validate(capability)
                self.assertEqual(validated.exception.code, "invalid_state")

    def test_active_fallback_capabilities_without_descriptors_are_valid(self) -> None:
        self.root.mkdir(parents=True)
        task_path = self.root / "task"
        task_path.mkdir()
        (task_path / "prompt.txt").write_text("prompt", encoding="utf-8")
        store = StateStore(self.root)
        parent = state_module._StateParent(
            self.root.parent,
            state_module._stat_identity(self.root.parent.lstat()),
        )
        root = state_module._StateRoot(
            self.root,
            state_module._stat_identity(self.root.lstat()),
            parent=parent,
        )
        task = state_module._TaskDirectory(
            task_path,
            state_module._stat_identity(task_path.lstat()),
            root=root,
        )

        self.assertEqual(store._read_state_text(task, "prompt.txt"), "prompt")
        _close_capability_for_test(task)

    def test_clone_root_failure_closes_only_new_parent_descriptor(self) -> None:
        store = StateStore(self.root)
        parent = state_module._StateParent(self.root.parent, (1, 2, 3), 101)
        root = state_module._StateRoot(self.root, (4, 5, 6), 202, parent)

        with patch.object(store, "_validate_root"), patch.object(
            store, "_validate_parent"
        ), patch.object(
            state_module.os,
            "dup",
            side_effect=[303, OSError(errno.EIO, "root dup failed")],
        ), patch.object(state_module.os, "close") as close:
            with self.assertRaises(OSError):
                store._clone_root(root)

        close.assert_called_once_with(303)
        self.assertFalse(getattr(root, "closed", False))
        self.assertFalse(getattr(parent, "closed", False))
        self.assertEqual(root.file_descriptor, 202)
        self.assertEqual(parent.file_descriptor, 101)

    def test_symlink_task_directory_is_rejected_before_return_or_create(self) -> None:
        self.root.mkdir(parents=True)
        target = Path(self.temporary_directory.name) / "target"
        target.mkdir()
        link = self.root / "linked-task"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)
        store = StateStore(self.root)

        for operation in (
            lambda: store.task_dir("linked-task"),
            lambda: store.create_task_dir("linked-task"),
            lambda: store.read_manifest(link),
            lambda: store.write_manifest(link, {"task_id": "linked-task"}),
            lambda: store.read_prompt(link),
            lambda: store.write_prompt(link, "prompt"),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(WorkerError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, "invalid_task_id")


class StateStorePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.store = StateStore(Path(self.temporary_directory.name) / "state")
        self.task_dir = self.store.create_task_dir("20260717-120000-a1b2c3d4")

    def test_terminal_manifest_snapshot_is_immutable(self) -> None:
        terminal = {
            "task_id": self.task_dir.name,
            "status": "cancelled",
            "error": "first terminal writer",
        }
        self.store.write_manifest(self.task_dir, terminal)

        self.store.write_manifest(
            self.task_dir,
            {
                "task_id": self.task_dir.name,
                "status": "cancelled",
                "error": "late same-status overwrite",
            },
        )
        retained = self.store.update_manifest(
            self.task_dir,
            lambda current: {
                **current,
                "status": "orphaned",
                "error": "late competing terminal",
            },
        )

        self.assertEqual(retained, terminal)
        self.assertEqual(self.store.read_manifest(self.task_dir), terminal)

    def test_cross_process_terminal_writers_have_one_immutable_winner(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "running"},
        )
        barrier = self.task_dir.parent / "writer-barrier"
        source_root = str(Path(__file__).resolve().parents[1] / "src")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (source_root, environment.get("PYTHONPATH")))
        )
        script = (
            "import sys,time\n"
            "from pathlib import Path\n"
            "from codex_worker_dispatcher.state import StateStore\n"
            "root=Path(sys.argv[1])\n"
            "task=StateStore(root).task_dir(sys.argv[2])\n"
            "barrier=Path(sys.argv[3])\n"
            "deadline=time.monotonic()+10\n"
            "while not barrier.exists():\n"
            "  assert time.monotonic() < deadline\n"
            "  time.sleep(0.01)\n"
            "StateStore(root).write_manifest(task, "
            "{'task_id':sys.argv[2], 'status':sys.argv[4], 'winner':sys.argv[4]})\n"
        )
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    script,
                    str(self.store.root),
                    self.task_dir.name,
                    str(barrier),
                    status,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                creationflags=windows_no_window_flags(),
            )
            for status in ("cancelled", "reaped")
        ]
        barrier.write_text("go", encoding="utf-8")
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, stdout + stderr)

        winner = self.store.read_manifest(self.task_dir)
        self.assertIn(winner["status"], {"cancelled", "reaped"})
        self.assertEqual(winner["winner"], winner["status"])
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "running"},
        )
        self.assertEqual(self.store.read_manifest(self.task_dir), winner)

    def test_manifest_lock_is_regular_nonempty_and_rejects_directory(self) -> None:
        manifest = {"task_id": self.task_dir.name, "status": "running"}
        self.store.write_manifest(self.task_dir, manifest)
        lock_path = self.task_dir / ".manifest.lock"

        lock_stat = lock_path.lstat()
        self.assertTrue(stat.S_ISREG(lock_stat.st_mode))
        self.assertGreaterEqual(lock_stat.st_size, 1)

        lock_path.unlink()
        lock_path.mkdir()
        with self.assertRaises(WorkerError) as raised:
            self.store.write_manifest(self.task_dir, manifest)
        self.assertEqual(raised.exception.code, "invalid_state")

    def test_manifest_lock_rejects_hardlink_without_touching_external_inode(
        self,
    ) -> None:
        external = Path(self.temporary_directory.name) / "external-lock"
        external.write_bytes(b"")
        lock_path = self.task_dir / ".manifest.lock"
        try:
            os.link(external, lock_path)
        except (AttributeError, NotImplementedError) as error:
            self.skipTest(f"Hardlink API unavailable: {error}")

        with self.assertRaises(WorkerError) as raised:
            self.store.write_manifest(
                self.task_dir,
                {"task_id": self.task_dir.name, "status": "running"},
            )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(external.read_bytes(), b"")

    def test_manifest_lock_is_released_when_writer_process_crashes(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "running"},
        )
        marker = self.task_dir.parent / "lock-acquired"
        source_root = str(Path(__file__).resolve().parents[1] / "src")
        environment = os.environ.copy()
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (source_root, environment.get("PYTHONPATH")))
        )
        script = (
            "import os,sys\n"
            "from pathlib import Path\n"
            "from codex_worker_dispatcher.state import StateStore\n"
            "store=StateStore(Path(sys.argv[1]))\n"
            "task=store.task_dir(sys.argv[2])\n"
            "with store._existing_task_dir(task) as capability:\n"
            "  with store._manifest_lock(capability):\n"
            "    Path(sys.argv[3]).write_text('locked', encoding='utf-8')\n"
            "    os._exit(0)\n"
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(self.store.root),
                self.task_dir.name,
                str(marker),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            creationflags=windows_no_window_flags(),
        )
        stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(marker.read_text(encoding="utf-8"), "locked")

        updated = self.store.update_manifest(
            self.task_dir,
            lambda current: {**current, "after_crash": True},
        )
        self.assertTrue(updated["after_crash"])

    def _lexical_parent_alias(self) -> Path:
        (self.store.root / "alias").mkdir()
        return self.store.root / "alias" / ".." / self.task_dir.name

    def _external_root_alias(self) -> Path:
        alias = Path(self.temporary_directory.name) / "outside-alias"
        try:
            alias.symlink_to(self.store.root, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)
        return alias / self.task_dir.name

    def _symlink_state_file(self, filename: str, content: str) -> tuple[Path, Path]:
        external_dir = Path(self.temporary_directory.name) / "external-state"
        external_dir.mkdir(exist_ok=True)
        target = external_dir / filename
        target.write_text(content, encoding="utf-8")
        link = self.task_dir / filename
        try:
            link.symlink_to(target)
        except OSError as error:
            _handle_symlink_setup_error(self, error)
        return link, target

    def _external_task_dir(self) -> Path:
        external_task_dir = Path(self.temporary_directory.name) / "external-task"
        external_task_dir.mkdir()
        (external_task_dir / "prompt.txt").write_text(
            "external prompt", encoding="utf-8"
        )
        (external_task_dir / "manifest.json").write_text(
            json.dumps({"task_id": self.task_dir.name, "status": "external"}),
            encoding="utf-8",
        )
        return external_task_dir

    def _external_state_root(self) -> Path:
        external_root = Path(self.temporary_directory.name) / "external-root"
        external_task_dir = external_root / self.task_dir.name
        external_task_dir.mkdir(parents=True)
        (external_task_dir / "prompt.txt").write_text(
            "external root prompt", encoding="utf-8"
        )
        (external_task_dir / "manifest.json").write_text(
            json.dumps(
                {"task_id": self.task_dir.name, "status": "external-root"}
            ),
            encoding="utf-8",
        )
        return external_root

    def _swap_state_root_for_symlink(self, target_root: Path) -> Path:
        moved_root = Path(self.temporary_directory.name) / "moved-state-root"
        self.store.root.replace(moved_root)
        try:
            self.store.root.symlink_to(target_root, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)
        return moved_root

    def _assert_operation_rejects_root_swap_before_task_access(
        self,
        operation: Callable[[], object],
    ) -> None:
        external_root = self._external_state_root()
        external_task = external_root / self.task_dir.name
        original_prompt = (external_task / "prompt.txt").read_bytes()
        original_manifest = (external_task / "manifest.json").read_bytes()
        real_task_entry_lstat = self.store._task_entry_lstat
        swapped = False

        def swapping_task_entry_lstat(
            root: object,
            task_id: str,
        ) -> os.stat_result:
            nonlocal swapped
            if not swapped and task_id == self.task_dir.name:
                self._swap_state_root_for_symlink(external_root)
                swapped = True
            return real_task_entry_lstat(root, task_id)  # type: ignore[arg-type]

        raised: WorkerError | None = None
        with patch.object(
            self.store,
            "_task_entry_lstat",
            side_effect=swapping_task_entry_lstat,
        ):
            try:
                operation()
            except WorkerError as error:
                raised = error

        self.assertTrue(swapped)
        self.assertEqual((external_task / "prompt.txt").read_bytes(), original_prompt)
        self.assertEqual(
            (external_task / "manifest.json").read_bytes(), original_manifest
        )
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "invalid_state")  # type: ignore[union-attr]

    def _assert_write_rejects_root_alias_after_temp_creation(
        self,
        filename: str,
        operation: Callable[[], object],
    ) -> None:
        target = self.task_dir / filename
        original = target.read_bytes()
        moved_root_path = Path(self.temporary_directory.name) / "moved-state-root"
        real_open = os.open
        real_mkstemp = tempfile.mkstemp
        moved_root: Path | None = None
        swapped = False

        def inject_swap() -> Path:
            nonlocal moved_root, swapped
            if swapped:
                raise AssertionError("state root was swapped more than once")
            moved_root = self._swap_state_root_for_symlink(moved_root_path)
            swapped = True
            return moved_root

        def swapping_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            file_descriptor, temp_path = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
            os.close(file_descriptor)
            moved = inject_swap()
            reopened = real_open(
                moved / self.task_dir.name / Path(temp_path).name,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
            return reopened, temp_path

        def swapping_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if dir_fd is None:
                file_descriptor = real_open(path, flags, mode)
            else:
                file_descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            basename = Path(os.fsdecode(path)).name
            if (
                not swapped
                and flags & os.O_CREAT
                and basename.startswith(f".{filename}.")
                and basename.endswith(".tmp")
            ):
                inject_swap()
            return file_descriptor

        if os.name == "nt":
            boundary_patch = patch.object(
                state_module.tempfile, "mkstemp", side_effect=swapping_mkstemp
            )
        else:
            boundary_patch = patch.object(
                state_module.os, "open", side_effect=swapping_open
            )
        raised: WorkerError | None = None
        with boundary_patch:
            try:
                operation()
            except WorkerError as error:
                raised = error

        if not swapped:
            self.assertIsNotNone(raised)
            self.assertEqual(raised.code, "invalid_state")  # type: ignore[union-attr]
            self.assertEqual(target.read_bytes(), original)
            return
        self.assertTrue(swapped)
        self.assertIsNotNone(moved_root)
        moved_target = moved_root / self.task_dir.name / filename  # type: ignore[operator]
        self.assertEqual(moved_target.read_bytes(), original)
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "invalid_state")  # type: ignore[union-attr]

    def _swap_task_dir_for_symlink(self, external_task_dir: Path) -> Path:
        moved_task_dir = Path(self.temporary_directory.name) / "moved-task"
        self.task_dir.replace(moved_task_dir)
        try:
            self.task_dir.symlink_to(external_task_dir, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)
        return moved_task_dir

    def _assert_read_rejects_task_dir_swap(
        self,
        operation: Callable[[], object],
    ) -> None:
        external_task_dir = self._external_task_dir()
        original_read = self.store._read_state_text
        swapped = False

        def swapping_read(*args: object, **kwargs: object) -> str:
            nonlocal swapped
            if not swapped:
                swapped = True
                self._swap_task_dir_for_symlink(external_task_dir)
            return original_read(*args, **kwargs)  # type: ignore[arg-type]

        with patch.object(self.store, "_read_state_text", side_effect=swapping_read):
            with self.assertRaises(WorkerError) as raised:
                operation()

        self.assertTrue(swapped)
        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(
            (external_task_dir / "prompt.txt").read_text(encoding="utf-8"),
            "external prompt",
        )
        self.assertEqual(
            json.loads(
                (external_task_dir / "manifest.json").read_text(encoding="utf-8")
            )["status"],
            "external",
        )

    def _assert_write_rejects_task_dir_swap(
        self,
        filename: str,
        operation: Callable[[], object],
    ) -> None:
        external_task_dir = self._external_task_dir()
        external_target = external_task_dir / filename
        original_external_content = external_target.read_bytes()
        real_open = os.open
        real_mkstemp = tempfile.mkstemp
        external_temp: Path | None = None
        swapped = False

        def inject_swap(basename: str) -> Path:
            nonlocal external_temp, swapped
            if swapped:
                raise AssertionError("task directory was swapped more than once")
            moved_task_dir = self._swap_task_dir_for_symlink(external_task_dir)
            external_temp = external_task_dir / basename
            external_temp.write_text("attacker temp", encoding="utf-8")
            swapped = True
            return moved_task_dir

        def swapping_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            file_descriptor, temp_name = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
            os.close(file_descriptor)
            basename = Path(temp_name).name
            moved_task_dir = inject_swap(basename)
            reopened_descriptor = real_open(
                moved_task_dir / basename,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
            return reopened_descriptor, temp_name

        def swapping_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal external_temp, swapped
            if dir_fd is None:
                file_descriptor = real_open(path, flags, mode)
            else:
                file_descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            basename = Path(os.fsdecode(path)).name
            if (
                not swapped
                and flags & os.O_CREAT
                and basename.startswith(f".{filename}.")
                and basename.endswith(".tmp")
            ):
                inject_swap(basename)
            return file_descriptor

        raised: WorkerError | None = None
        if os.name == "nt":
            boundary_patch = patch.object(
                state_module.tempfile,
                "mkstemp",
                side_effect=swapping_mkstemp,
            )
        else:
            boundary_patch = patch.object(
                state_module.os,
                "open",
                side_effect=swapping_open,
            )
        with boundary_patch:
            try:
                operation()
            except WorkerError as error:
                raised = error

        if not swapped:
            self.assertIsNotNone(raised)
            self.assertEqual(raised.code, "invalid_state")  # type: ignore[union-attr]
            self.assertEqual(external_target.read_bytes(), original_external_content)
            return
        self.assertTrue(swapped)
        self.assertEqual(external_target.read_bytes(), original_external_content)
        self.assertIsNotNone(external_temp)
        self.assertTrue(external_temp.exists())  # type: ignore[union-attr]
        self.assertEqual(
            external_temp.read_text(encoding="utf-8"),  # type: ignore[union-attr]
            "attacker temp",
        )
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "invalid_state")  # type: ignore[union-attr]

    def test_read_prompt_rejects_task_dir_swap_after_validation(self) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")

        self._assert_read_rejects_task_dir_swap(
            lambda: self.store.read_prompt(self.task_dir)
        )

    def test_read_rejects_closed_task_capability(self) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")
        task = self.store._existing_task_dir(self.task_dir)
        _close_capability_for_test(task)

        with self.assertRaises(WorkerError) as raised:
            self.store._read_state_text(task, "prompt.txt")

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_read_rejects_task_with_closed_root_capability(self) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")
        task = self.store._existing_task_dir(self.task_dir)
        self.assertIsNotNone(task.root)
        task.root.close()  # type: ignore[union-attr]
        try:
            with self.assertRaises(WorkerError) as raised:
                self.store._read_state_text(task, "prompt.txt")
        finally:
            _close_capability_for_test(task)

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_closed_task_rejects_temp_creation_before_filesystem_access(self) -> None:
        task = self.store._existing_task_dir(self.task_dir)
        _close_capability_for_test(task)

        with patch.object(
            state_module, "_supports_relative_replace", return_value=False
        ), patch.object(
            state_module.tempfile,
            "mkstemp",
            wraps=tempfile.mkstemp,
        ) as mkstemp:
            with self.assertRaises(WorkerError) as raised:
                self.store._create_temp_file(task, "prompt.txt")

        self.assertEqual(raised.exception.code, "invalid_state")
        mkstemp.assert_not_called()

    def test_closed_owner_rejects_fallback_temp_creation_before_mkstemp(self) -> None:
        for closed_owner in ("root", "parent"):
            with self.subTest(closed_owner=closed_owner):
                parent = state_module._StateParent(
                    self.store.root.parent,
                    state_module._stat_identity(self.store.root.parent.lstat()),
                )
                root = state_module._StateRoot(
                    self.store.root,
                    state_module._stat_identity(self.store.root.lstat()),
                    parent=parent,
                )
                task = state_module._TaskDirectory(
                    self.task_dir,
                    state_module._stat_identity(self.task_dir.lstat()),
                    root=root,
                )
                before = sorted(entry.name for entry in self.task_dir.iterdir())
                owner = root if closed_owner == "root" else parent
                _close_capability_for_test(owner)

                with patch.object(
                    state_module, "_supports_relative_replace", return_value=False
                ), patch.object(
                    state_module.tempfile,
                    "mkstemp",
                    side_effect=WorkerError(
                        "invalid_state", "unexpected mkstemp", {}
                    ),
                ) as mkstemp:
                    with self.assertRaises(WorkerError) as raised:
                        self.store._create_temp_file(task, "prompt.txt")

                self.assertFalse(task.closed)
                self.assertEqual(raised.exception.code, "invalid_state")
                mkstemp.assert_not_called()
                self.assertEqual(
                    sorted(entry.name for entry in self.task_dir.iterdir()),
                    before,
                )
                _close_capability_for_test(task)

    def test_closed_owner_rejects_relative_temp_creation_before_open(self) -> None:
        for closed_owner in ("root", "parent"):
            with self.subTest(closed_owner=closed_owner), patch.object(
                state_module.os, "close"
            ):
                parent = state_module._StateParent(
                    self.store.root.parent,
                    state_module._stat_identity(self.store.root.parent.lstat()),
                    101,
                )
                root = state_module._StateRoot(
                    self.store.root,
                    state_module._stat_identity(self.store.root.lstat()),
                    202,
                    parent,
                )
                task = state_module._TaskDirectory(
                    self.task_dir,
                    state_module._stat_identity(self.task_dir.lstat()),
                    303,
                    root,
                )
                owner = root if closed_owner == "root" else parent
                _close_capability_for_test(owner)

                with patch.object(
                    state_module, "_supports_relative_replace", return_value=True
                ), patch.object(
                    state_module.os,
                    "open",
                    side_effect=WorkerError("invalid_state", "unexpected open", {}),
                ) as open_file:
                    with self.assertRaises(WorkerError) as raised:
                        self.store._create_temp_file(task, "prompt.txt")

                self.assertFalse(task.closed)
                self.assertEqual(raised.exception.code, "invalid_state")
                open_file.assert_not_called()
                _close_capability_for_test(task)

    def test_read_manifest_rejects_task_dir_swap_after_validation(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )

        self._assert_read_rejects_task_dir_swap(
            lambda: self.store.read_manifest(self.task_dir)
        )

    def test_list_manifests_rejects_task_dir_swap_before_read(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )

        self._assert_read_rejects_task_dir_swap(self.store.list_manifests)

    def test_write_prompt_rejects_task_dir_swap_after_temp_creation(self) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")

        self._assert_write_rejects_task_dir_swap(
            "prompt.txt",
            lambda: self.store.write_prompt(self.task_dir, "replacement prompt"),
        )

    def test_write_manifest_rejects_task_dir_swap_after_temp_creation(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )

        self._assert_write_rejects_task_dir_swap(
            "manifest.json",
            lambda: self.store.write_manifest(
                self.task_dir,
                {"task_id": self.task_dir.name, "status": "replacement"},
            ),
        )

    def test_read_prompt_rejects_root_swap_before_task_access(self) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")

        self._assert_operation_rejects_root_swap_before_task_access(
            lambda: self.store.read_prompt(self.task_dir)
        )

    def test_read_manifest_rejects_root_swap_before_task_access(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )

        self._assert_operation_rejects_root_swap_before_task_access(
            lambda: self.store.read_manifest(self.task_dir)
        )

    def test_list_manifests_rejects_root_swap_before_task_access(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )

        self._assert_operation_rejects_root_swap_before_task_access(
            self.store.list_manifests
        )

    def test_write_prompt_rejects_root_swap_before_task_access(self) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")

        self._assert_operation_rejects_root_swap_before_task_access(
            lambda: self.store.write_prompt(self.task_dir, "replacement prompt")
        )

    def test_write_manifest_rejects_root_swap_before_task_access(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )

        self._assert_operation_rejects_root_swap_before_task_access(
            lambda: self.store.write_manifest(
                self.task_dir,
                {"task_id": self.task_dir.name, "status": "replacement"},
            )
        )

    def test_write_prompt_rejects_root_alias_after_temp_creation(self) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")

        self._assert_write_rejects_root_alias_after_temp_creation(
            "prompt.txt",
            lambda: self.store.write_prompt(self.task_dir, "replacement prompt"),
        )

    def test_write_manifest_rejects_root_alias_after_temp_creation(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )

        self._assert_write_rejects_root_alias_after_temp_creation(
            "manifest.json",
            lambda: self.store.write_manifest(
                self.task_dir,
                {"task_id": self.task_dir.name, "status": "replacement"},
            ),
        )

    def test_read_prompt_rejects_state_file_symlink(self) -> None:
        _, target = self._symlink_state_file("prompt.txt", "external prompt")

        with self.assertRaises(WorkerError) as raised:
            self.store.read_prompt(self.task_dir)

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(target.read_text(encoding="utf-8"), "external prompt")

    def test_read_manifest_and_listing_reject_state_file_symlink(self) -> None:
        external_manifest = json.dumps(
            {"task_id": self.task_dir.name, "status": "external"}
        )
        _, target = self._symlink_state_file("manifest.json", external_manifest)

        for operation in (
            lambda: self.store.read_manifest(self.task_dir),
            self.store.list_manifests,
        ):
            with self.subTest(operation=operation), patch("time.sleep") as sleep:
                with self.assertRaises(WorkerError) as raised:
                    operation()

                self.assertEqual(raised.exception.code, "invalid_state")
                sleep.assert_not_called()

        self.assertEqual(target.read_text(encoding="utf-8"), external_manifest)

    def test_writes_reject_state_file_symlinks_without_touching_targets(self) -> None:
        cases = (
            (
                "prompt.txt",
                "external prompt",
                lambda: self.store.write_prompt(self.task_dir, "replacement prompt"),
            ),
            (
                "manifest.json",
                json.dumps({"task_id": self.task_dir.name, "status": "external"}),
                lambda: self.store.write_manifest(
                    self.task_dir,
                    {"task_id": self.task_dir.name, "status": "replacement"},
                ),
            ),
        )

        for filename, external_content, operation in cases:
            with self.subTest(filename=filename):
                link, target = self._symlink_state_file(filename, external_content)

                with self.assertRaises(WorkerError) as raised:
                    operation()

                self.assertEqual(raised.exception.code, "invalid_state")
                self.assertTrue(link.is_symlink())
                self.assertEqual(target.read_text(encoding="utf-8"), external_content)

    def test_state_file_directories_are_invalid_state(self) -> None:
        prompt_path = self.task_dir / "prompt.txt"
        manifest_path = self.task_dir / "manifest.json"
        prompt_path.mkdir()
        manifest_path.mkdir()

        operations = (
            lambda: self.store.read_prompt(self.task_dir),
            lambda: self.store.write_prompt(self.task_dir, "replacement prompt"),
            lambda: self.store.read_manifest(self.task_dir),
            lambda: self.store.write_manifest(
                self.task_dir,
                {"task_id": self.task_dir.name, "status": "replacement"},
            ),
            self.store.list_manifests,
        )
        for operation in operations:
            with self.subTest(operation=operation), patch("time.sleep"):
                with self.assertRaises(WorkerError) as raised:
                    operation()

                self.assertEqual(raised.exception.code, "invalid_state")

        self.assertTrue(prompt_path.is_dir())
        self.assertTrue(manifest_path.is_dir())

    def test_read_prompt_rejects_external_parent_symlink_before_access(self) -> None:
        self.store.write_prompt(self.task_dir, "private prompt")
        aliased_task_dir = self._external_root_alias()
        raised: WorkerError | None = None

        with patch.object(
            self.store, "_read_state_text", return_value="private prompt"
        ) as read_text:
            try:
                self.store.read_prompt(aliased_task_dir)
            except WorkerError as error:
                raised = error

        read_text.assert_not_called()
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "invalid_task_id")  # type: ignore[union-attr]

    def test_write_prompt_rejects_external_parent_symlink_without_mutation(
        self,
    ) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")
        prompt_path = self.task_dir / "prompt.txt"
        original = prompt_path.read_bytes()
        aliased_task_dir = self._external_root_alias()
        raised: WorkerError | None = None

        try:
            self.store.write_prompt(aliased_task_dir, "mutated prompt")
        except WorkerError as error:
            raised = error

        self.assertEqual(prompt_path.read_bytes(), original)
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "invalid_task_id")  # type: ignore[union-attr]

    def test_read_prompt_rejects_lexical_parent_before_file_access(self) -> None:
        self.store.write_prompt(self.task_dir, "private prompt")
        aliased_task_dir = self._lexical_parent_alias()
        raised: WorkerError | None = None

        with patch.object(
            self.store, "_read_state_text", return_value="private prompt"
        ) as read_text:
            try:
                self.store.read_prompt(aliased_task_dir)
            except WorkerError as error:
                raised = error

        read_text.assert_not_called()
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "invalid_task_id")  # type: ignore[union-attr]

    def test_write_prompt_rejects_lexical_parent_without_mutation(self) -> None:
        self.store.write_prompt(self.task_dir, "original prompt")
        prompt_path = self.task_dir / "prompt.txt"
        original = prompt_path.read_bytes()
        aliased_task_dir = self._lexical_parent_alias()
        raised: WorkerError | None = None

        try:
            self.store.write_prompt(aliased_task_dir, "mutated prompt")
        except WorkerError as error:
            raised = error

        self.assertEqual(prompt_path.read_bytes(), original)
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])
        self.assertIsNotNone(raised)
        self.assertEqual(raised.code, "invalid_task_id")  # type: ignore[union-attr]

    def _outside_task_dirs(self) -> tuple[Path, Path]:
        sibling = self.store.root.parent / "sibling-task"
        escaped = self.store.root / ".." / "outside" / "escaped-task"
        for task_dir in (sibling, escaped):
            task_dir.mkdir(parents=True)
            (task_dir / "prompt.txt").write_text(
                "outside prompt", encoding="utf-8"
            )
            (task_dir / "manifest.json").write_text(
                json.dumps({"task_id": task_dir.name, "status": "outside"}),
                encoding="utf-8",
            )
        return sibling, escaped

    def test_read_methods_reject_task_directories_outside_store_root(self) -> None:
        for task_dir in self._outside_task_dirs():
            for operation in (
                lambda: self.store.read_prompt(task_dir),
                lambda: self.store.read_manifest(task_dir),
            ):
                with self.subTest(task_dir=task_dir, operation=operation), patch(
                    "time.sleep"
                ):
                    with self.assertRaises(WorkerError) as raised:
                        operation()
                    self.assertEqual(raised.exception.code, "invalid_task_id")

    def test_write_methods_reject_outside_directories_without_mutation(self) -> None:
        for task_dir in self._outside_task_dirs():
            prompt_path = task_dir / "prompt.txt"
            manifest_path = task_dir / "manifest.json"
            original_prompt = prompt_path.read_bytes()
            original_manifest = manifest_path.read_bytes()

            operations = (
                lambda: self.store.write_prompt(task_dir, "mutated prompt"),
                lambda: self.store.write_manifest(
                    task_dir,
                    {"task_id": task_dir.name, "status": "mutated"},
                ),
            )
            for operation in operations:
                with self.subTest(task_dir=task_dir, operation=operation):
                    with self.assertRaises(WorkerError) as raised:
                        operation()
                    self.assertEqual(raised.exception.code, "invalid_task_id")

            self.assertEqual(prompt_path.read_bytes(), original_prompt)
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertEqual(list(task_dir.glob("*.tmp")), [])

    def test_prompt_and_manifest_round_trip_exactly_without_temps(self) -> None:
        prompt = "inspect parser"
        manifest: dict[str, object] = {
            "task_id": self.task_dir.name,
            "status": "queued",
            "message": "中文",
        }

        self.store.write_prompt(self.task_dir, prompt)
        self.store.write_manifest(self.task_dir, manifest)

        self.assertEqual(self.store.read_prompt(self.task_dir), prompt)
        self.assertEqual(self.store.read_manifest(self.task_dir), manifest)
        self.assertEqual(
            (self.task_dir / "manifest.json").read_text(encoding="utf-8"),
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_manifest_write_fsyncs_before_unique_same_directory_replace(self) -> None:
        real_fsync = os.fsync
        real_replace = os.replace
        events: list[tuple[str, object]] = []

        def recording_fsync(file_descriptor: int) -> None:
            events.append(("fsync", file_descriptor))
            real_fsync(file_descriptor)

        def recording_replace(
            source: str,
            destination: str,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            events.append(
                ("replace", (source, destination, src_dir_fd, dst_dir_fd))
            )
            if src_dir_fd is None and dst_dir_fd is None:
                real_replace(source, destination)
            else:
                real_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

        def recording_directory_fsync(
            path: Path,
            file_descriptor: int | None = None,
        ) -> None:
            events.append(("directory-fsync", path))

        with patch(
            "codex_worker_dispatcher.state.os.fsync", side_effect=recording_fsync
        ), patch(
            "codex_worker_dispatcher.state.os.replace", side_effect=recording_replace
        ), patch(
            "codex_worker_dispatcher.state._fsync_directory",
            side_effect=recording_directory_fsync,
        ):
            self.store.write_manifest(
                self.task_dir, {"task_id": self.task_dir.name, "status": "first"}
            )
            self.store.write_manifest(
                self.task_dir, {"task_id": self.task_dir.name, "status": "second"}
            )

        self.assertEqual(
            [event[0] for event in events],
            ["fsync", "replace", "directory-fsync"] * 2,
        )
        replacements = [event[1] for event in events if event[0] == "replace"]
        sources = [Path(replacement[0]) for replacement in replacements]  # type: ignore[index]
        destinations = [Path(replacement[1]) for replacement in replacements]  # type: ignore[index]
        self.assertNotEqual(sources[0], sources[1])
        if replacements[0][2] is None:  # type: ignore[index]
            self.assertTrue(all(source.parent == self.task_dir for source in sources))
            self.assertEqual(destinations, [self.task_dir / "manifest.json"] * 2)
        else:
            self.assertTrue(all(source.parent == Path(".") for source in sources))
            self.assertEqual(destinations, [Path("manifest.json")] * 2)
            self.assertTrue(
                all(
                    replacement[2] == replacement[3]  # type: ignore[index]
                    for replacement in replacements
                )
            )

    def test_atomic_write_accepts_later_writer_after_replace(self) -> None:
        later = {"task_id": self.task_dir.name, "status": "later-writer"}
        later_path = self.task_dir / "later-writer.json"
        later_path.write_text(json.dumps(later), encoding="utf-8")
        real_replace = os.replace
        later_replaced = False

        def replace_then_later(
            source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal later_replaced
            real_replace(source, destination, *args, **kwargs)  # type: ignore[arg-type]
            if not later_replaced and Path(os.fsdecode(destination)).name == "manifest.json":
                real_replace(later_path, destination)
                later_replaced = True

        write_error: WorkerError | None = None
        with patch.object(
            state_module, "_supports_relative_replace", return_value=False
        ), patch.object(state_module.os, "replace", side_effect=replace_then_later):
            try:
                self.store.write_manifest(
                    self.task_dir,
                    {"task_id": self.task_dir.name, "status": "our-writer"},
                )
            except WorkerError as error:
                write_error = error

        self.assertIsNone(write_error)
        self.assertTrue(later_replaced)
        self.assertEqual(self.store.read_manifest(self.task_dir), later)
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_atomic_write_retries_windows_replace_conflicts_with_fresh_temps(
        self,
    ) -> None:
        if os.name != "nt":
            self.skipTest("Windows-specific replace conflicts")
        expected = {"task_id": self.task_dir.name, "status": "recovered"}
        real_replace = os.replace
        replace_sources: list[Path] = []
        conflicts = [
            OSError(errno.EBUSY, "busy"),
            OSError("sharing violation"),
        ]
        conflicts[1].winerror = 32  # type: ignore[attr-defined]

        def flaky_replace(
            source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            *args: object,
            **kwargs: object,
        ) -> None:
            replace_sources.append(Path(os.fsdecode(source)))
            if conflicts:
                raise conflicts.pop(0)
            real_replace(source, destination, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(
            state_module, "_supports_relative_replace", return_value=False
        ), patch.object(
            state_module.os, "replace", side_effect=flaky_replace
        ), patch.object(
            state_module.tempfile, "mkstemp", wraps=tempfile.mkstemp
        ) as mkstemp, patch("time.sleep") as sleep:
            self.store.write_manifest(self.task_dir, expected)

        self.assertEqual(len(replace_sources), 3)
        self.assertEqual(len({source.name for source in replace_sources}), 3)
        self.assertEqual(mkstemp.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.025)] * 2)
        self.assertEqual(self.store.read_manifest(self.task_dir), expected)
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_atomic_write_keeps_permanent_permission_failure_invalid_state(
        self,
    ) -> None:
        if os.name != "nt":
            self.skipTest("Windows-specific replace conflicts")
        original = {"task_id": self.task_dir.name, "status": "original"}
        self.store.write_manifest(self.task_dir, original)

        with patch.object(
            state_module, "_supports_relative_replace", return_value=False
        ), patch.object(
            state_module.os,
            "replace",
            side_effect=PermissionError(errno.EPERM, "permanently denied"),
        ) as replace, patch("time.sleep") as sleep:
            with self.assertRaises(WorkerError) as raised:
                self.store.write_manifest(
                    self.task_dir,
                    {"task_id": self.task_dir.name, "status": "replacement"},
                )

        self.assertEqual(raised.exception.code, "invalid_state")
        replace.assert_called_once()
        sleep.assert_not_called()
        self.assertEqual(self.store.read_manifest(self.task_dir), original)
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_eight_public_manifest_writers_are_last_writer_wins(self) -> None:
        writer_count = 8
        writes_per_writer = 6
        barrier = Barrier(writer_count)

        def write_many(writer: int) -> WorkerError | None:
            barrier.wait()
            for iteration in range(writes_per_writer):
                try:
                    self.store.write_manifest(
                        self.task_dir,
                        {
                            "task_id": self.task_dir.name,
                            "writer": writer,
                            "iteration": iteration,
                        },
                    )
                except WorkerError as error:
                    return error
            return None

        with ThreadPoolExecutor(max_workers=writer_count) as executor:
            errors = [
                error
                for error in executor.map(write_many, range(writer_count))
                if error is not None
            ]

        manifest = self.store.read_manifest(self.task_dir)
        self.assertEqual(errors, [])
        self.assertEqual(manifest["task_id"], self.task_dir.name)
        self.assertIsInstance(manifest.get("writer"), int)
        self.assertIsInstance(manifest.get("iteration"), int)
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_directory_fsync_failure_is_invalid_state_without_temp_files(self) -> None:
        with patch(
            "codex_worker_dispatcher.state._fsync_directory",
            side_effect=OSError(errno.EIO, "directory fsync failed"),
        ):
            with self.assertRaises(WorkerError) as raised:
                self.store.write_manifest(
                    self.task_dir,
                    {"task_id": self.task_dir.name, "status": "running"},
                )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_temp_fstat_failure_is_invalid_state_without_temp_files(self) -> None:
        real_fstat = os.fstat
        temp_fstat_calls = 0
        with self.store._existing_task_dir(self.task_dir) as task:
            directory_descriptors = {
                descriptor
                for descriptor in (
                    task.file_descriptor,
                    task.root.file_descriptor if task.root is not None else None,
                    (
                        task.root.parent.file_descriptor
                        if task.root is not None and task.root.parent is not None
                        else None
                    ),
                )
                if descriptor is not None
            }

            def fail_temp_fstat(file_descriptor: int) -> os.stat_result:
                nonlocal temp_fstat_calls
                if file_descriptor in directory_descriptors:
                    return real_fstat(file_descriptor)
                temp_fstat_calls += 1
                if temp_fstat_calls == 1:
                    raise OSError(errno.EIO, "temp fstat failed")
                return real_fstat(file_descriptor)

            with patch.object(
                state_module, "_supports_relative_replace", return_value=False
            ), patch.object(
                state_module.os, "fstat", side_effect=fail_temp_fstat
            ):
                with self.assertRaises(WorkerError) as raised:
                    self.store._write_atomic_text(
                        task, "prompt.txt", "replacement prompt"
                    )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(temp_fstat_calls, 2)
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_temp_replaced_before_entry_stat_does_not_remove_bait(self) -> None:
        real_mkstemp = tempfile.mkstemp
        real_fstat = os.fstat
        bait = b"replacement before entry stat"
        temp_path: Path | None = None
        created_stat: os.stat_result | None = None

        def replacing_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            nonlocal created_stat, temp_path
            file_descriptor, name = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
            temp_path = Path(name)
            created_stat = real_fstat(file_descriptor)
            os.close(file_descriptor)
            temp_path.unlink()
            temp_path.write_bytes(bait)
            return file_descriptor, name

        with self.store._existing_task_dir(self.task_dir) as task:
            directory_descriptors = {
                descriptor
                for descriptor in (
                    task.file_descriptor,
                    task.root.file_descriptor if task.root is not None else None,
                    (
                        task.root.parent.file_descriptor
                        if task.root is not None and task.root.parent is not None
                        else None
                    ),
                )
                if descriptor is not None
            }

            def created_file_fstat(file_descriptor: int) -> os.stat_result:
                if file_descriptor in directory_descriptors:
                    return real_fstat(file_descriptor)
                assert created_stat is not None
                return created_stat

            with patch.object(
                state_module, "_supports_relative_replace", return_value=False
            ), patch.object(
                state_module.tempfile, "mkstemp", side_effect=replacing_mkstemp
            ), patch.object(
                state_module.os, "fstat", side_effect=created_file_fstat
            ):
                with self.assertRaises(WorkerError) as raised:
                    self.store._write_atomic_text(
                        task, "prompt.txt", "replacement prompt"
                    )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertIsNotNone(temp_path)
        self.assertTrue(temp_path.exists())  # type: ignore[union-attr]
        self.assertEqual(temp_path.read_bytes(), bait)  # type: ignore[union-attr]

    def test_continuous_temp_fstat_failure_does_not_remove_bait(self) -> None:
        real_mkstemp = tempfile.mkstemp
        real_fstat = os.fstat
        bait = b"replacement bait"
        temp_path: Path | None = None
        temp_fstat_calls = 0

        def recording_mkstemp(*args: object, **kwargs: object) -> tuple[int, str]:
            nonlocal temp_path
            file_descriptor, name = real_mkstemp(*args, **kwargs)  # type: ignore[arg-type]
            temp_path = Path(name)
            return file_descriptor, name

        def replace_before_fstat(
            file_descriptor: int,
            directory_descriptors: set[int],
        ) -> os.stat_result:
            nonlocal temp_fstat_calls
            if file_descriptor in directory_descriptors:
                return real_fstat(file_descriptor)
            temp_fstat_calls += 1
            if temp_fstat_calls == 1:
                os.close(file_descriptor)
                assert temp_path is not None
                temp_path.unlink()
                temp_path.write_bytes(bait)
            raise OSError(errno.EIO, "temp fstat failed")

        with self.store._existing_task_dir(self.task_dir) as task:
            directory_descriptors = {
                descriptor
                for descriptor in (
                    task.file_descriptor,
                    task.root.file_descriptor if task.root is not None else None,
                    (
                        task.root.parent.file_descriptor
                        if task.root is not None and task.root.parent is not None
                        else None
                    ),
                )
                if descriptor is not None
            }
            with patch.object(
                state_module, "_supports_relative_replace", return_value=False
            ), patch.object(
                state_module.tempfile, "mkstemp", side_effect=recording_mkstemp
            ), patch.object(
                state_module.os,
                "fstat",
                side_effect=lambda descriptor: replace_before_fstat(
                    descriptor, directory_descriptors
                ),
            ):
                with self.assertRaises(WorkerError) as raised:
                    self.store._write_atomic_text(
                        task, "prompt.txt", "replacement prompt"
                    )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(temp_fstat_calls, 2)
        self.assertIsNotNone(temp_path)
        self.assertEqual(temp_path.read_bytes(), bait)  # type: ignore[union-attr]

    def test_relative_temp_cleanup_retries_fd_identity_after_fstat_error(self) -> None:
        fd_reference = self.task_dir / "fd-reference.tmp"
        fd_reference.write_text("fd reference", encoding="utf-8")
        fd_stat = fd_reference.lstat()
        entry_reference = self.task_dir / "entry-reference.tmp"
        entry_reference.write_text("entry reference", encoding="utf-8")
        entry_stat = entry_reference.lstat()
        task = state_module._TaskDirectory(
            path=self.task_dir,
            identity=state_module._stat_identity(self.task_dir.lstat()),
            file_descriptor=123,
        )

        with patch.object(
            self.store, "_validate_task_dir"
        ) as validate_task_dir, patch.object(
            state_module, "_supports_relative_replace", return_value=True
        ), patch.object(
            state_module.secrets, "token_hex", return_value="token"
        ), patch.object(
            state_module.os, "open", return_value=456
        ), patch.object(
            state_module.os, "stat", return_value=entry_stat
        ) as entry_lstat, patch.object(
            state_module.os,
            "fstat",
            side_effect=[OSError(errno.EIO, "temp fstat failed"), fd_stat],
        ), patch.object(state_module.os, "close"), patch.object(
            self.store, "_cleanup_temp_file"
        ) as cleanup:
            with self.assertRaises(OSError):
                self.store._create_temp_file(task, "prompt.txt")

        validate_task_dir.assert_called_once_with(task)
        cleanup.assert_called_once_with(
            task,
            ".prompt.txt.token.tmp",
            state_module._stat_identity(fd_stat),
            True,
        )
        entry_lstat.assert_not_called()

    def test_directory_fsync_ignores_only_explicit_unsupported_errors(self) -> None:
        unsupported_errors = {errno.EINVAL, errno.ENOSYS, errno.ENOTSUP}

        for error_number in unsupported_errors:
            with self.subTest(error_number=error_number), patch.object(
                state_module.os, "name", "posix"
            ), patch.object(state_module.os, "open", return_value=123), patch.object(
                state_module.os,
                "fsync",
                side_effect=OSError(error_number, "unsupported"),
            ), patch.object(state_module.os, "close") as close:
                state_module._fsync_directory(self.task_dir)

                close.assert_called_once_with(123)

    def test_directory_fsync_returns_without_opening_on_windows(self) -> None:
        with patch.object(state_module.os, "name", "nt"), patch.object(
            state_module.os, "open"
        ) as open_directory:
            state_module._fsync_directory(self.task_dir)

        open_directory.assert_not_called()

    def test_atomic_write_cleans_temp_and_preserves_manifest_on_failure(self) -> None:
        original = {"task_id": self.task_dir.name, "status": "queued"}
        self.store.write_manifest(self.task_dir, original)

        with patch(
            "codex_worker_dispatcher.state.os.replace",
            side_effect=OSError("replace failed"),
        ):
            with self.assertRaises(WorkerError) as raised:
                self.store.write_manifest(
                    self.task_dir,
                    {"task_id": self.task_dir.name, "status": "running"},
                )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(self.store.read_manifest(self.task_dir), original)
        self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_prompt_write_uses_atomic_text_path(self) -> None:
        real_replace = os.replace
        replacements: list[tuple[str, str, int | None, int | None]] = []

        def recording_replace(
            source: str,
            destination: str,
            *,
            src_dir_fd: int | None = None,
            dst_dir_fd: int | None = None,
        ) -> None:
            replacements.append((source, destination, src_dir_fd, dst_dir_fd))
            if src_dir_fd is None and dst_dir_fd is None:
                real_replace(source, destination)
            else:
                real_replace(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )

        with patch(
            "codex_worker_dispatcher.state.os.replace", side_effect=recording_replace
        ) as replace, patch(
            "codex_worker_dispatcher.state.os.fsync"
        ) as fsync, patch("codex_worker_dispatcher.state._fsync_directory"):
            self.store.write_prompt(self.task_dir, "exact\ncontent  ")

        fsync.assert_called_once()
        replace.assert_called_once()
        if replacements[0][2] is None:
            self.assertEqual(Path(replacements[0][0]).parent, self.task_dir)
            self.assertEqual(Path(replacements[0][1]), self.task_dir / "prompt.txt")
        else:
            self.assertEqual(Path(replacements[0][0]).parent, Path("."))
            self.assertEqual(Path(replacements[0][1]), Path("prompt.txt"))
            self.assertEqual(replacements[0][2], replacements[0][3])
        self.assertEqual(self.store.read_prompt(self.task_dir), "exact\ncontent  ")

    def test_manifest_read_retries_transient_invalid_json_until_success(self) -> None:
        expected = {"task_id": self.task_dir.name}
        responses = ["{"] * 19 + [json.dumps(expected)]

        with patch.object(
            self.store, "_read_state_text", side_effect=responses
        ) as read_text, patch("time.sleep") as sleep:
            manifest = self.store.read_manifest(self.task_dir)

        self.assertEqual(manifest, expected)
        self.assertEqual(read_text.call_count, 20)
        self.assertEqual(sleep.call_args_list, [call(0.025)] * 19)

    def test_manifest_read_retries_transient_identity_mismatch(self) -> None:
        expected = {"task_id": self.task_dir.name, "status": "recovered"}
        responses = [json.dumps({"task_id": "other-task"})] * 19 + [
            json.dumps(expected)
        ]

        with patch.object(
            self.store, "_read_state_text", side_effect=responses
        ) as read_text, patch("time.sleep") as sleep:
            manifest = self.store.read_manifest(self.task_dir)

        self.assertEqual(manifest, expected)
        self.assertEqual(read_text.call_count, 20)
        self.assertEqual(sleep.call_args_list, [call(0.025)] * 19)

    def test_manifest_read_retries_atomic_replace_between_lstat_and_open(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )
        expected = {"task_id": self.task_dir.name, "status": "replacement"}
        manifest_path = self.task_dir / "manifest.json"
        replacement = self.task_dir / "manifest-next.json"
        replacement.write_text(json.dumps(expected), encoding="utf-8")
        real_open = os.open
        replaced = False

        def replacing_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replaced
            if not replaced and Path(os.fsdecode(path)).name == "manifest.json":
                replacement.replace(manifest_path)
                replaced = True
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with patch.object(
            state_module.os, "open", side_effect=replacing_open
        ), patch.object(
            self.store, "_read_state_text", wraps=self.store._read_state_text
        ) as read_text, patch("time.sleep") as sleep:
            manifest = self.store.read_manifest(self.task_dir)

        self.assertTrue(replaced)
        self.assertEqual(manifest, expected)
        self.assertEqual(read_text.call_count, 2)
        sleep.assert_called_once_with(0.025)

    def test_manifest_read_stops_after_persistent_atomic_replace_churn(self) -> None:
        self.store.write_manifest(
            self.task_dir,
            {"task_id": self.task_dir.name, "status": "original"},
        )
        manifest_path = self.task_dir / "manifest.json"
        replacements: list[Path] = []
        for attempt in range(20):
            replacement = self.task_dir / f"manifest-next-{attempt}.json"
            replacement.write_text(
                json.dumps(
                    {
                        "task_id": self.task_dir.name,
                        "status": f"replacement-{attempt}",
                    }
                ),
                encoding="utf-8",
            )
            replacements.append(replacement)
        real_open = os.open
        replacement_count = 0

        def churning_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal replacement_count
            if (
                Path(os.fsdecode(path)).name == "manifest.json"
                and replacement_count < len(replacements)
            ):
                replacements[replacement_count].replace(manifest_path)
                replacement_count += 1
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with patch.object(
            state_module.os, "open", side_effect=churning_open
        ), patch.object(
            self.store, "_read_state_text", wraps=self.store._read_state_text
        ) as read_text, patch("time.sleep") as sleep:
            with self.assertRaises(WorkerError) as raised:
                self.store.read_manifest(self.task_dir)

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(replacement_count, 20)
        self.assertEqual(read_text.call_count, 20)
        self.assertEqual(sleep.call_args_list, [call(0.025)] * 19)

    def test_missing_manifest_retries_then_raises_task_not_found(self) -> None:
        with patch.object(
            self.store,
            "_read_state_text",
            side_effect=FileNotFoundError("missing"),
        ) as read_text, patch("time.sleep") as sleep:
            with self.assertRaises(WorkerError) as raised:
                self.store.read_manifest(self.task_dir)

        self.assertEqual(raised.exception.code, "task_not_found")
        self.assertEqual(read_text.call_count, 20)
        self.assertEqual(sleep.call_args_list, [call(0.025)] * 19)

    def test_persistently_invalid_or_non_object_manifest_is_invalid_state(self) -> None:
        for response in ("{", "[]"):
            with self.subTest(response=response), patch.object(
                self.store, "_read_state_text", return_value=response
            ) as read_text, patch("time.sleep") as sleep:
                with self.assertRaises(WorkerError) as raised:
                    self.store.read_manifest(self.task_dir)

                self.assertEqual(raised.exception.code, "invalid_state")
                self.assertEqual(read_text.call_count, 20)
                self.assertEqual(sleep.call_args_list, [call(0.025)] * 19)

    def test_missing_non_string_or_mismatched_manifest_identity_is_invalid_state(
        self,
    ) -> None:
        for manifest in ({}, {"task_id": 1}, {"task_id": "other-task"}):
            with self.subTest(manifest=manifest), patch.object(
                self.store,
                "_read_state_text",
                return_value=json.dumps(manifest),
            ) as read_text, patch("time.sleep") as sleep:
                with self.assertRaises(WorkerError) as raised:
                    self.store.read_manifest(self.task_dir)

                self.assertEqual(raised.exception.code, "invalid_state")
                self.assertEqual(read_text.call_count, 20)
                self.assertEqual(sleep.call_args_list, [call(0.025)] * 19)

    def test_write_manifest_rejects_invalid_identity_without_mutation(self) -> None:
        original = {"task_id": self.task_dir.name, "status": "queued"}
        self.store.write_manifest(self.task_dir, original)
        manifest_path = self.task_dir / "manifest.json"
        original_bytes = manifest_path.read_bytes()

        for manifest in ({}, {"task_id": 1}, {"task_id": "other-task"}):
            with self.subTest(manifest=manifest):
                with self.assertRaises(WorkerError) as raised:
                    self.store.write_manifest(self.task_dir, manifest)

                self.assertEqual(raised.exception.code, "invalid_state")
                self.assertEqual(manifest_path.read_bytes(), original_bytes)
                self.assertEqual(list(self.task_dir.glob("*.tmp")), [])

    def test_manifest_io_failure_is_invalid_state(self) -> None:
        with patch.object(
            self.store,
            "_read_state_text",
            side_effect=PermissionError("denied"),
        ) as read_text, patch("time.sleep") as sleep:
            with self.assertRaises(WorkerError) as raised:
                self.store.read_manifest(self.task_dir)

        self.assertEqual(raised.exception.code, "invalid_state")
        read_text.assert_called_once()
        sleep.assert_not_called()

    def test_manifest_retries_transient_windows_read_conflict(self) -> None:
        conflict = PermissionError(errno.EACCES, "sharing violation")
        expected = {"task_id": self.task_dir.name, "status": "queued"}

        with patch.object(
            state_module,
            "_is_retryable_windows_file_conflict",
            return_value=True,
        ), patch.object(
            self.store,
            "_read_state_text",
            side_effect=[conflict, json.dumps(expected)],
        ) as read_text, patch("time.sleep") as sleep:
            manifest = self.store.read_manifest(self.task_dir)

        self.assertEqual(manifest, expected)
        self.assertEqual(read_text.call_count, 2)
        sleep.assert_called_once_with(0.025)

    def test_manifest_bounds_persistent_windows_read_conflict(self) -> None:
        conflict = PermissionError(errno.EACCES, "sharing violation")

        with patch.object(
            state_module,
            "_is_retryable_windows_file_conflict",
            return_value=True,
        ), patch.object(
            self.store,
            "_read_state_text",
            side_effect=conflict,
        ) as read_text, patch("time.sleep") as sleep:
            with self.assertRaises(WorkerError) as raised:
                self.store.read_manifest(self.task_dir)

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(read_text.call_count, 20)
        self.assertEqual(sleep.call_args_list, [call(0.025)] * 19)

    def test_manifest_does_not_retry_non_conflict_io_failure(self) -> None:
        with patch.object(
            state_module,
            "_is_retryable_windows_file_conflict",
            return_value=False,
        ), patch.object(
            self.store,
            "_read_state_text",
            side_effect=OSError(errno.EIO, "device error"),
        ) as read_text, patch("time.sleep") as sleep:
            with self.assertRaises(WorkerError) as raised:
                self.store.read_manifest(self.task_dir)

        self.assertEqual(raised.exception.code, "invalid_state")
        read_text.assert_called_once()
        sleep.assert_not_called()

    def test_absent_task_manifest_and_prompt_use_task_not_found(self) -> None:
        missing_task = self.store.root / "missing-task"
        operations = (
            lambda: self.store.read_manifest(missing_task),
            lambda: self.store.write_manifest(missing_task, {"task_id": "missing-task"}),
            lambda: self.store.read_prompt(missing_task),
            lambda: self.store.write_prompt(missing_task, "prompt"),
            lambda: self.store.read_manifest(self.task_dir),
            lambda: self.store.read_prompt(self.task_dir),
        )

        with patch("time.sleep"):
            for operation in operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(WorkerError) as raised:
                        operation()
                    self.assertEqual(raised.exception.code, "task_not_found")

    def test_prompt_io_failure_is_invalid_state(self) -> None:
        with patch.object(
            self.store,
            "_read_state_text",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(WorkerError) as raised:
                self.store.read_prompt(self.task_dir)

        self.assertEqual(raised.exception.code, "invalid_state")


class StateStoreListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name) / "state"
        self.store = StateStore(self.root)

    def test_list_manifests_is_empty_without_creating_root(self) -> None:
        self.assertEqual(self.store.list_manifests(), [])
        self.assertFalse(self.root.exists())

    def test_list_manifests_sorts_and_skips_unrelated_directories(self) -> None:
        beta = self.store.create_task_dir("beta")
        alpha = self.store.create_task_dir("alpha")
        self.store.write_manifest(beta, {"task_id": "beta", "status": "running"})
        self.store.write_manifest(alpha, {"task_id": "alpha", "status": "queued"})

        self.store.create_task_dir("missing-manifest")
        (self.root / "plain-file").write_text("ignore", encoding="utf-8")
        nested = self.root / "container" / "nested-task"
        nested.mkdir(parents=True)
        (nested / "manifest.json").write_text(
            '{"task_id": "nested-task"}', encoding="utf-8"
        )

        with patch("time.sleep") as sleep:
            manifests = self.store.list_manifests()

        self.assertEqual(
            manifests,
            [
                {"task_id": "alpha", "status": "queued"},
                {"task_id": "beta", "status": "running"},
            ],
        )
        sleep.assert_not_called()

    def test_list_manifests_propagates_corrupt_manifest(self) -> None:
        corrupt = self.store.create_task_dir("corrupt-manifest")
        (corrupt / "manifest.json").write_text("{", encoding="utf-8")

        with patch("time.sleep"):
            with self.assertRaises(WorkerError) as raised:
                self.store.list_manifests()

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_list_manifests_propagates_unreadable_manifest(self) -> None:
        task_dir = self.store.create_task_dir("unreadable-manifest")
        self.store.write_manifest(task_dir, {"task_id": task_dir.name})

        with patch.object(
            self.store,
            "_read_state_text",
            side_effect=PermissionError("denied"),
        ):
            with self.assertRaises(WorkerError) as raised:
                self.store.list_manifests()

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_list_manifests_propagates_identity_mismatch(self) -> None:
        task_dir = self.store.create_task_dir("mismatched-manifest")
        (task_dir / "manifest.json").write_text(
            '{"task_id": "other-task"}', encoding="utf-8"
        )

        with patch("time.sleep"):
            with self.assertRaises(WorkerError) as raised:
                self.store.list_manifests()

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_list_manifests_rejects_dangling_manifest_symlink(self) -> None:
        task_dir = self.store.create_task_dir("dangling-manifest")
        manifest_path = task_dir / "manifest.json"
        missing_target = Path(self.temporary_directory.name) / "missing.json"
        try:
            manifest_path.symlink_to(missing_target)
        except OSError as error:
            _handle_symlink_setup_error(self, error)
        self.assertTrue(manifest_path.is_symlink())
        self.assertFalse(manifest_path.exists())

        with patch("time.sleep"):
            with self.assertRaises(WorkerError) as raised:
                self.store.list_manifests()

        self.assertEqual(raised.exception.code, "invalid_state")

    def test_list_manifests_skips_invalid_id_directory_without_manifest(self) -> None:
        (self.root / "Bad").mkdir(parents=True)

        self.assertEqual(self.store.list_manifests(), [])

    def test_list_manifests_rejects_invalid_id_directory_with_manifest(self) -> None:
        invalid_task_dir = self.root / "Bad"
        invalid_task_dir.mkdir(parents=True)
        manifest_path = invalid_task_dir / "manifest.json"

        for content in ("{", '{"task_id": "Bad"}'):
            with self.subTest(content=content):
                manifest_path.write_text(content, encoding="utf-8")
                with patch.object(
                    self.store,
                    "_read_state_text",
                    side_effect=AssertionError("invalid task manifest was read"),
                ) as read_text:
                    with self.assertRaises(WorkerError) as raised:
                        self.store.list_manifests()

                self.assertEqual(raised.exception.code, "invalid_state")
                read_text.assert_not_called()

    def test_list_manifests_rejects_symlink_directory_with_manifest(self) -> None:
        self.root.mkdir(parents=True)
        target = Path(self.temporary_directory.name) / "target-task"
        target.mkdir()
        (target / "manifest.json").write_text(
            '{"task_id": "linked-task"}', encoding="utf-8"
        )
        link = self.root / "linked-task"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)

        with patch.object(
            self.store,
            "_read_state_text",
            side_effect=AssertionError("symlink manifest was read"),
        ) as read_text:
            with self.assertRaises(WorkerError) as raised:
                self.store.list_manifests()

        self.assertEqual(raised.exception.code, "invalid_state")
        read_text.assert_not_called()

    def test_list_manifests_rejects_symlink_without_accessing_target(self) -> None:
        self.root.mkdir(parents=True)
        target = Path(self.temporary_directory.name) / "target-without-manifest"
        target.mkdir()
        link = self.root / "linked-task"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError as error:
            _handle_symlink_setup_error(self, error)

        real_lstat = Path.lstat
        target_accesses: list[Path] = []
        target_paths = {target, target / "manifest.json", link / "manifest.json"}

        def recording_lstat(path: Path) -> os.stat_result:
            if path in target_paths:
                target_accesses.append(path)
            return real_lstat(path)

        with patch.object(
            Path, "lstat", autospec=True, side_effect=recording_lstat
        ):
            with self.assertRaises(WorkerError) as raised:
                self.store.list_manifests()

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(target_accesses, [])

    def test_list_manifests_reports_root_io_state_failure(self) -> None:
        self.root.parent.mkdir(parents=True, exist_ok=True)
        self.root.write_text("not a directory", encoding="utf-8")

        with self.assertRaises(WorkerError) as raised:
            self.store.list_manifests()

        self.assertEqual(raised.exception.code, "invalid_state")


if __name__ == "__main__":
    unittest.main()
