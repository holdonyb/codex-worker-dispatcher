from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from codex_worker_dispatcher import __version__, cli
from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.installer import (
    MARKER_NAME,
    default_skill_target,
    install_skill,
    uninstall_skill,
)


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.root = Path(temporary_directory.name)
        self.source = self.root / "bundle" / "dispatching-codex-workers"
        (self.source / "agents").mkdir(parents=True)
        (self.source / "references").mkdir()
        (self.source / "SKILL.md").write_text("original skill\n", encoding="utf-8")
        (self.source / "agents" / "openai.yaml").write_text(
            "interface:\n  display_name: Worker\n",
            encoding="utf-8",
        )
        (self.source / "references" / "design.md").write_text(
            "# Design\n",
            encoding="utf-8",
        )
        self.target = self.root / "home" / ".agents" / "skills" / "dispatching-codex-workers"

    def _make_directory_reparse(self, link: Path, target: Path) -> None:
        if os.name == "nt":
            completed = subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                check=False,
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=10,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        else:
            link.symlink_to(target, target_is_directory=True)

    def _install(self, *, target: Path | None = None, upgrade: bool = False) -> dict[str, object]:
        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ):
            return install_skill(target=target or self.target, upgrade=upgrade)

    def test_fresh_install_copies_bundle_and_writes_ownership_marker(self) -> None:
        result = self._install()

        self.assertEqual(result["action"], "installed")
        self.assertEqual(result["target"], str(self.target))
        self.assertEqual((self.target / "SKILL.md").read_text(encoding="utf-8"), "original skill\n")
        marker = json.loads((self.target / MARKER_NAME).read_text(encoding="utf-8"))
        self.assertEqual(marker["owner"], "codex-worker-dispatcher")
        self.assertEqual(marker["version"], __version__)
        self.assertEqual(
            marker["source"],
            str(Path(os.path.abspath(os.fspath(self.source)))),
        )
        self.assertEqual(list(self.target.parent.glob(".*.install-*")), [])

    @unittest.skipIf(os.name == "nt", "POSIX directory mode contract")
    def test_install_keeps_target_root_private_after_copy(self) -> None:
        self._install()

        self.assertEqual(stat.S_IMODE(self.target.lstat().st_mode), 0o700)

    def test_identical_reinstall_is_idempotent(self) -> None:
        self._install()

        result = self._install()

        self.assertEqual(result["action"], "unchanged")
        self.assertNotIn("backup", result)
        self.assertEqual(list(self.target.parent.glob(f"{self.target.name}.backup-*")), [])

    def test_modified_existing_install_is_refused_without_upgrade(self) -> None:
        self._install()
        (self.target / "SKILL.md").write_text("locally modified\n", encoding="utf-8")

        with self.assertRaises(WorkerError) as raised:
            self._install()

        self.assertEqual(raised.exception.code, "install_conflict")
        self.assertEqual(
            (self.target / "SKILL.md").read_text(encoding="utf-8"),
            "locally modified\n",
        )

    def test_upgrade_keeps_timestamped_backup_of_existing_directory(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "SKILL.md").write_text("unrelated old skill\n", encoding="utf-8")

        result = self._install(upgrade=True)

        backup = Path(str(result["backup"]))
        self.assertEqual(result["action"], "upgraded")
        self.assertTrue(backup.is_dir())
        self.assertEqual(backup.parent, self.target.parent)
        self.assertTrue(backup.name.startswith(f"{self.target.name}.backup-"))
        self.assertEqual(
            (backup / "SKILL.md").read_text(encoding="utf-8"),
            "unrelated old skill\n",
        )
        self.assertEqual((self.target / "SKILL.md").read_text(encoding="utf-8"), "original skill\n")

    def test_copy_failure_restores_original_directory_and_removes_temporary_files(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "keep.txt").write_text("keep me\n", encoding="utf-8")

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer.shutil.copytree",
            side_effect=OSError("simulated copy failure"),
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(target=self.target, upgrade=True)

        self.assertEqual(raised.exception.code, "install_failed")
        self.assertEqual((self.target / "keep.txt").read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(list(self.target.parent.glob(f"{self.target.name}.backup-*")), [])
        self.assertEqual(list(self.target.parent.glob(".*.install-*")), [])

    def test_staging_cleanup_failure_does_not_prevent_backup_restore(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "keep.txt").write_text("keep me\n", encoding="utf-8")

        def fail_after_partial_copy(
            _source: Path,
            staging: Path,
            *args: object,
            **kwargs: object,
        ) -> None:
            del args, kwargs
            staging.mkdir(exist_ok=True)
            (staging / "partial.txt").write_text("partial\n", encoding="utf-8")
            raise OSError("simulated partial copy failure")

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer.shutil.copytree",
            side_effect=fail_after_partial_copy,
        ), patch(
            "codex_worker_dispatcher.installer._safe_rmtree",
            side_effect=OSError("simulated cleanup failure"),
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(target=self.target, upgrade=True)

        self.assertEqual(raised.exception.code, "install_failed")
        self.assertEqual((self.target / "keep.txt").read_text(encoding="utf-8"), "keep me\n")
        self.assertEqual(list(self.target.parent.glob(f"{self.target.name}.backup-*")), [])

    def test_owned_uninstall_removes_installed_directory(self) -> None:
        self._install()

        result = uninstall_skill(self.target)

        self.assertEqual(result, {"action": "uninstalled", "target": str(self.target)})
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.target.parent.glob(".*.remove-*")), [])

    def test_uninstall_never_delegates_recursive_removal_to_shutil_rmtree(self) -> None:
        self._install()

        with patch(
            "codex_worker_dispatcher.installer.shutil.rmtree",
            side_effect=AssertionError("path-based rmtree is unsafe on Windows"),
        ) as rmtree:
            result = uninstall_skill(self.target)

        self.assertEqual(result["action"], "uninstalled")
        rmtree.assert_not_called()

    def test_uninstall_detects_tombstone_root_swap_to_reparse_before_recursion(self) -> None:
        self._install()
        external = self.root / "external-removal-root"
        external.mkdir()
        secret = external / "keep.txt"
        secret.write_text("keep me\n", encoding="utf-8")
        displaced = self.root / "displaced-removal-root"
        real_scandir = os.scandir
        real_rename = os.rename
        tombstone_scans = 0

        def swap_root_on_recursive_scan(path: object) -> object:
            nonlocal tombstone_scans
            if not isinstance(path, int):
                candidate = Path(path)  # type: ignore[arg-type]
                if candidate.name.startswith(f".{self.target.name}.remove-"):
                    tombstone_scans += 1
                    if tombstone_scans == 3:
                        real_rename(candidate, displaced)
                        self._make_directory_reparse(candidate, external)
            return real_scandir(path)  # type: ignore[arg-type]

        with patch(
            "codex_worker_dispatcher.installer.os.scandir",
            side_effect=swap_root_on_recursive_scan,
        ):
            with self.assertRaises(WorkerError) as raised:
                uninstall_skill(self.target)

        self.assertEqual(raised.exception.code, "uninstall_failed")
        self.assertEqual(secret.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue((displaced / "SKILL.md").is_file())

    def test_uninstall_detects_child_swap_to_reparse_before_recursion(self) -> None:
        self._install()
        external = self.root / "external-removal-child"
        external.mkdir()
        secret = external / "keep.txt"
        secret.write_text("keep me\n", encoding="utf-8")
        displaced = self.root / "displaced-removal-child"
        real_scandir = os.scandir
        real_rename = os.rename
        child_scans = 0

        def swap_child_on_recursive_scan(path: object) -> object:
            nonlocal child_scans
            if not isinstance(path, int):
                candidate = Path(path)  # type: ignore[arg-type]
                if (
                    candidate.name == "agents"
                    and candidate.parent.name.startswith(
                        f".{self.target.name}.remove-"
                    )
                ):
                    child_scans += 1
                    if child_scans == 3:
                        real_rename(candidate, displaced)
                        self._make_directory_reparse(candidate, external)
            return real_scandir(path)  # type: ignore[arg-type]

        with patch(
            "codex_worker_dispatcher.installer.os.scandir",
            side_effect=swap_child_on_recursive_scan,
        ):
            with self.assertRaises(WorkerError) as raised:
                uninstall_skill(self.target)

        self.assertEqual(raised.exception.code, "uninstall_failed")
        self.assertEqual(secret.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue((displaced / "openai.yaml").is_file())

    def test_uninstall_detects_ordinary_child_directory_swap_after_preflight(self) -> None:
        self._install()
        displaced = self.root / "displaced-ordinary-child"
        real_scandir = os.scandir
        real_rename = os.rename
        tombstone_scans = 0
        replacement: Path | None = None

        def swap_child_after_preflight(path: object) -> object:
            nonlocal replacement, tombstone_scans
            if not isinstance(path, int):
                candidate = Path(path)  # type: ignore[arg-type]
                if candidate.name.startswith(f".{self.target.name}.remove-"):
                    tombstone_scans += 1
                    if tombstone_scans == 3:
                        original_child = candidate / "agents"
                        real_rename(original_child, displaced)
                        original_child.mkdir()
                        replacement = original_child
                        (original_child / "do-not-delete.txt").write_text(
                            "unrelated\n",
                            encoding="utf-8",
                        )
            return real_scandir(path)  # type: ignore[arg-type]

        with patch(
            "codex_worker_dispatcher.installer.os.scandir",
            side_effect=swap_child_after_preflight,
        ):
            with self.assertRaises(WorkerError) as raised:
                uninstall_skill(self.target)

        self.assertEqual(raised.exception.code, "uninstall_failed")
        self.assertIsNotNone(replacement)
        if replacement is not None:
            self.assertEqual(
                (replacement / "do-not-delete.txt").read_text(encoding="utf-8"),
                "unrelated\n",
            )
        self.assertTrue((displaced / "openai.yaml").is_file())

    def test_uninstall_rollback_never_restores_replaced_ordinary_tombstone(self) -> None:
        self._install()
        displaced = self.root / "displaced-owned-tombstone"
        real_rename = os.rename
        replacement: Path | None = None

        def replace_tombstone_then_fail(
            tombstone: Path,
            *,
            expected_identity: tuple[int, int, int],
            expected_marker: dict[str, object] | None = None,
        ) -> None:
            nonlocal replacement
            del expected_identity, expected_marker
            real_rename(tombstone, displaced)
            tombstone.mkdir()
            replacement = tombstone
            (tombstone / "do-not-move.txt").write_text(
                "unrelated\n",
                encoding="utf-8",
            )
            raise OSError("simulated removal failure after tombstone replacement")

        with patch(
            "codex_worker_dispatcher.installer._safe_rmtree",
            side_effect=replace_tombstone_then_fail,
        ):
            with self.assertRaises(WorkerError) as raised:
                uninstall_skill(self.target)

        self.assertEqual(raised.exception.code, "uninstall_failed")
        details = dict(raised.exception.details)
        self.assertFalse(details["tombstone_restored"])
        self.assertIsNotNone(replacement)
        if replacement is not None:
            self.assertEqual(Path(str(details["tombstone"])), replacement)
            self.assertEqual(
                (replacement / "do-not-move.txt").read_text(encoding="utf-8"),
                "unrelated\n",
            )
        self.assertFalse(self.target.exists())
        self.assertTrue((displaced / "SKILL.md").is_file())

    def test_partial_copy_failure_never_deletes_replaced_staging_directory(self) -> None:
        displaced_staging = self.root / "displaced-staging"
        replacement: Path | None = None

        def swap_partial_staging(
            _source: Path,
            staging: Path,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal replacement
            del args, kwargs
            staging.mkdir(exist_ok=True)
            (staging / "partial.txt").write_text("partial\n", encoding="utf-8")
            os.rename(staging, displaced_staging)
            staging.mkdir()
            replacement = staging
            (staging / "do-not-delete.txt").write_text(
                "unrelated\n",
                encoding="utf-8",
            )
            raise OSError("simulated copy failure after a staging swap")

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer.shutil.copytree",
            side_effect=swap_partial_staging,
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(self.target)

        self.assertEqual(raised.exception.code, "install_failed")
        self.assertIsNotNone(replacement)
        if replacement is not None:
            self.assertEqual(
                (replacement / "do-not-delete.txt").read_text(encoding="utf-8"),
                "unrelated\n",
            )
        self.assertTrue((displaced_staging / "partial.txt").is_file())

    def test_source_swap_during_copy_fails_closed(self) -> None:
        displaced_source = self.root / "displaced-source"
        external = self.root / "replacement-source"
        external.mkdir()
        (external / "SKILL.md").write_text("replacement\n", encoding="utf-8")
        secret = external / "secret.txt"
        secret.write_text("keep me\n", encoding="utf-8")
        real_copytree = shutil.copytree

        def swap_source_before_copy(
            source: Path,
            staging: Path,
            *args: object,
            **kwargs: object,
        ) -> Path:
            os.rename(source, displaced_source)
            self._make_directory_reparse(source, external)
            return real_copytree(source, staging, *args, **kwargs)

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer.shutil.copytree",
            side_effect=swap_source_before_copy,
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(self.target)

        self.assertEqual(raised.exception.code, "install_failed")
        self.assertFalse(self.target.exists())
        self.assertEqual(secret.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue((displaced_source / "SKILL.md").is_file())

    def test_marker_hardlink_in_staging_is_refused_without_overwriting_source(self) -> None:
        external_marker = self.root / "external-marker-source.json"
        original = b"do not overwrite\n"
        external_marker.write_bytes(original)
        real_copytree = shutil.copytree

        def add_hardlinked_marker(
            source: Path,
            staging: Path,
            *args: object,
            **kwargs: object,
        ) -> Path:
            copied = real_copytree(source, staging, *args, **kwargs)
            os.link(external_marker, staging / MARKER_NAME)
            return copied

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer.shutil.copytree",
            side_effect=add_hardlinked_marker,
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(self.target)

        self.assertEqual(raised.exception.code, "install_failed")
        self.assertEqual(external_marker.read_bytes(), original)
        self.assertFalse(self.target.exists())

    def test_marker_hardlink_race_at_exclusive_open_is_refused(self) -> None:
        external_marker = self.root / "external-marker-race.json"
        original = b"do not overwrite\n"
        external_marker.write_bytes(original)
        real_open = os.open
        injected = False

        def add_hardlink_before_open(
            path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            nonlocal injected
            candidate = Path(path)
            if not injected and candidate.name == MARKER_NAME:
                injected = True
                os.link(external_marker, candidate)
            if dir_fd is None:
                return real_open(path, flags, mode)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer.os.open",
            side_effect=add_hardlink_before_open,
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(self.target)

        self.assertEqual(raised.exception.code, "install_failed")
        self.assertTrue(injected)
        self.assertEqual(external_marker.read_bytes(), original)
        self.assertFalse(self.target.exists())

    def test_marker_symlink_in_staging_is_refused_without_overwriting_source(self) -> None:
        external_marker = self.root / "external-marker-source.json"
        original = b"do not overwrite\n"
        external_marker.write_bytes(original)
        real_copytree = shutil.copytree

        def add_symlinked_marker(
            source: Path,
            staging: Path,
            *args: object,
            **kwargs: object,
        ) -> Path:
            copied = real_copytree(source, staging, *args, **kwargs)
            try:
                (staging / MARKER_NAME).symlink_to(external_marker)
            except OSError as error:
                self.skipTest(f"File symlinks are unavailable: {error}")
            return copied

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer.shutil.copytree",
            side_effect=add_symlinked_marker,
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(self.target)

        self.assertEqual(raised.exception.code, "install_failed")
        self.assertEqual(external_marker.read_bytes(), original)
        self.assertFalse(self.target.exists())

    def test_destination_swap_during_final_rename_is_detected_and_preserved(self) -> None:
        displaced_staging = self.root / "displaced-complete-staging"
        real_rename = os.rename

        def replace_final_destination(
            source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        ) -> None:
            source_path = Path(source)
            destination_path = Path(destination)
            if (
                destination_path == self.target
                and source_path.name.startswith(f".{self.target.name}.install-")
            ):
                real_rename(source_path, displaced_staging)
                self.target.mkdir()
                (self.target / "do-not-delete.txt").write_text(
                    "unrelated\n",
                    encoding="utf-8",
                )
                return
            real_rename(source, destination)

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer.os.rename",
            side_effect=replace_final_destination,
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(self.target)

        self.assertEqual(raised.exception.code, "install_failed")
        self.assertEqual(
            (self.target / "do-not-delete.txt").read_text(encoding="utf-8"),
            "unrelated\n",
        )
        self.assertTrue((displaced_staging / "SKILL.md").is_file())

    def test_uninstall_refuses_directory_without_valid_ownership_marker(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "keep.txt").write_text("keep me\n", encoding="utf-8")

        with self.assertRaises(WorkerError) as raised:
            uninstall_skill(self.target)

        self.assertEqual(raised.exception.code, "uninstall_refused")
        self.assertEqual((self.target / "keep.txt").read_text(encoding="utf-8"), "keep me\n")

    def test_uninstall_rejects_hardlinked_ownership_marker_without_changing_source(self) -> None:
        self.target.mkdir(parents=True)
        external_marker = self.root / "external-marker.json"
        marker_bytes = json.dumps(
            {
                "schema_version": 1,
                "owner": "codex-worker-dispatcher",
                "version": __version__,
                "source": str(self.source),
            }
        ).encode("utf-8")
        external_marker.write_bytes(marker_bytes)
        os.link(external_marker, self.target / MARKER_NAME)
        (self.target / "keep.txt").write_text("keep me\n", encoding="utf-8")

        with self.assertRaises(WorkerError) as raised:
            uninstall_skill(self.target)

        self.assertEqual(raised.exception.code, "uninstall_refused")
        self.assertEqual(external_marker.read_bytes(), marker_bytes)
        self.assertEqual((self.target / "keep.txt").read_text(encoding="utf-8"), "keep me\n")

    def test_uninstall_detects_target_swap_after_validation_and_never_deletes_replacement(self) -> None:
        self._install()
        displaced = self.root / "displaced-owned-install"
        real_rename = os.rename
        swapped = False

        def swap_before_rename(source: str | bytes | os.PathLike[str] | os.PathLike[bytes], destination: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> None:
            nonlocal swapped
            source_path = Path(source)
            if not swapped and source_path == self.target:
                swapped = True
                real_rename(self.target, displaced)
                self.target.mkdir()
                shutil.copy2(displaced / MARKER_NAME, self.target / MARKER_NAME)
                (self.target / "do-not-delete.txt").write_text("unrelated\n", encoding="utf-8")
            real_rename(source, destination)

        with patch("codex_worker_dispatcher.installer.os.rename", side_effect=swap_before_rename):
            with self.assertRaises(WorkerError) as raised:
                uninstall_skill(self.target)

        self.assertIn(raised.exception.code, {"uninstall_refused", "uninstall_failed"})
        self.assertTrue(swapped)
        details = dict(raised.exception.details)
        self.assertFalse(details["tombstone_restored"])
        retained_tombstone = Path(str(details["tombstone"]))
        self.assertEqual(
            (retained_tombstone / "do-not-delete.txt").read_text(encoding="utf-8"),
            "unrelated\n",
        )
        self.assertFalse(self.target.exists())
        self.assertTrue((displaced / "SKILL.md").is_file())

    def test_source_directory_reparse_is_rejected_without_reading_external_secret(self) -> None:
        external = self.root / "external-source"
        external.mkdir()
        secret = external / "secret.txt"
        secret.write_text("never copy this\n", encoding="utf-8")
        self._make_directory_reparse(self.source / "external-link", external)

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(self.target)

        self.assertEqual(raised.exception.code, "skill_source_invalid")
        self.assertEqual(secret.read_text(encoding="utf-8"), "never copy this\n")
        self.assertFalse(self.target.exists())

    def test_target_directory_reparse_is_refused_even_for_upgrade(self) -> None:
        external = self.root / "external-target"
        external.mkdir()
        secret = external / "keep.txt"
        secret.write_text("keep me\n", encoding="utf-8")
        self.target.parent.mkdir(parents=True)
        self._make_directory_reparse(self.target, external)

        with self.assertRaises(WorkerError) as upgraded:
            self._install(upgrade=True)
        self.assertIn(upgraded.exception.code, {"install_conflict", "unsafe_target"})
        self.assertEqual(secret.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue(self.target.exists())

    def test_uninstall_refuses_target_directory_reparse(self) -> None:
        external = self.root / "external-target"
        external.mkdir()
        secret = external / "keep.txt"
        secret.write_text("keep me\n", encoding="utf-8")
        self.target.parent.mkdir(parents=True)
        self._make_directory_reparse(self.target, external)

        with self.assertRaises(WorkerError) as raised:
            uninstall_skill(self.target)

        self.assertEqual(raised.exception.code, "uninstall_refused")
        self.assertEqual(secret.read_text(encoding="utf-8"), "keep me\n")
        self.assertTrue(self.target.exists())

    def test_concurrent_target_during_upgrade_preserves_backup_and_reports_its_path(self) -> None:
        self.target.mkdir(parents=True)
        (self.target / "old.txt").write_text("original\n", encoding="utf-8")
        real_write_marker = __import__(
            "codex_worker_dispatcher.installer",
            fromlist=["_write_marker"],
        )._write_marker

        def create_concurrent_target(
            staging: Path,
            marker: dict[str, str | int],
            *,
            expected_directory_identity: tuple[int, int, int],
        ) -> None:
            real_write_marker(
                staging,
                marker,
                expected_directory_identity=expected_directory_identity,
            )
            self.target.mkdir()
            (self.target / "concurrent.txt").write_text("new owner\n", encoding="utf-8")

        with patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ), patch(
            "codex_worker_dispatcher.installer._write_marker",
            side_effect=create_concurrent_target,
        ):
            with self.assertRaises(WorkerError) as raised:
                install_skill(self.target, upgrade=True)

        self.assertEqual(raised.exception.code, "install_failed")
        details = dict(raised.exception.details)
        self.assertFalse(details["backup_restored"])
        backup = Path(str(details["backup"]))
        self.assertEqual((backup / "old.txt").read_text(encoding="utf-8"), "original\n")
        self.assertEqual(
            (self.target / "concurrent.txt").read_text(encoding="utf-8"),
            "new owner\n",
        )

    def test_default_install_uses_temporary_home_and_not_real_home(self) -> None:
        temporary_home = self.root / "isolated-home"
        expected = temporary_home / ".agents" / "skills" / "dispatching-codex-workers"

        with patch(
            "codex_worker_dispatcher.installer.Path.home",
            return_value=temporary_home,
        ), patch(
            "codex_worker_dispatcher.installer._bundled_skill_source",
            return_value=self.source,
        ):
            self.assertEqual(default_skill_target(), expected)
            result = install_skill()

        self.assertEqual(result["target"], str(expected))
        self.assertTrue((expected / "SKILL.md").is_file())
        self.assertFalse(self.target.exists())

    def test_install_cli_forwards_target_and_upgrade_and_writes_one_json_object(self) -> None:
        stdout = io.StringIO()
        expected = {"action": "upgraded", "target": str(self.target), "backup": "backup"}
        with patch.object(cli, "install_skill", return_value=expected) as mocked, redirect_stdout(stdout):
            exit_code = cli.main(
                ["skill", "install", "--target", str(self.target), "--upgrade"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True, **expected})
        mocked.assert_called_once_with(self.target, upgrade=True)

    def test_uninstall_cli_requires_yes_without_prompting(self) -> None:
        stdout = io.StringIO()
        with patch.object(cli, "uninstall_skill") as mocked, patch(
            "builtins.input",
            side_effect=AssertionError("CLI must not prompt"),
        ), redirect_stdout(stdout):
            exit_code = cli.main(["skill", "uninstall", "--target", str(self.target)])

        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "invalid_arguments")
        mocked.assert_not_called()

    def test_uninstall_cli_with_yes_calls_owned_uninstaller(self) -> None:
        stdout = io.StringIO()
        expected = {"action": "uninstalled", "target": str(self.target)}
        with patch.object(cli, "uninstall_skill", return_value=expected) as mocked, redirect_stdout(stdout):
            exit_code = cli.main(
                ["skill", "uninstall", "--target", str(self.target), "--yes"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), {"ok": True, **expected})
        mocked.assert_called_once_with(self.target)


if __name__ == "__main__":
    unittest.main()
