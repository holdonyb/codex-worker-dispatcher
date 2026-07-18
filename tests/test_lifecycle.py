from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from codex_worker_dispatcher import lifecycle
from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.process import (
    ProcessIdentity,
    owned_task_identity_matches,
    read_process_identity,
    terminate_owned_tree,
    wait_until_gone,
)
from codex_worker_dispatcher.lifecycle import (
    cancel_task,
    list_tasks,
    reap_stale_tasks,
    reap_task,
    result_task,
    route_task,
    start_task,
    status_task,
    wait_task,
)
from codex_worker_dispatcher.state import StateStore, TERMINAL_STATES, utc_now


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.workdir = root / "work"
        self.workdir.mkdir()
        self.state_root = root / "state"
        self.task_ids: list[str] = []
        self.addCleanup(self._cleanup_tasks)

    def _start(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "prompt": "FAKE_RESULT_OK",
            "workdir": self.workdir,
            "state_root": self.state_root,
            "engine": "fake",
            "fake_delay_sec": 0.1,
            "timeout_sec": 15,
        }
        arguments.update(overrides)
        started = start_task(**arguments)
        self.task_ids.append(str(started["task_id"]))
        return started

    def _cleanup_tasks(self) -> None:
        for task_id in reversed(self.task_ids):
            try:
                manifest = StateStore(self.state_root).read_manifest(
                    StateStore(self.state_root).task_dir(task_id)
                )
            except WorkerError as error:
                if error.code == "task_not_found":
                    continue
                raise
            if manifest.get("status") not in TERMINAL_STATES:
                reaped = reap_task(task_id, self.state_root)
                self.assertIn(reaped.get("status"), TERMINAL_STATES)

    def _manual_task(
        self,
        task_id: str,
        *,
        updated_at: str | None = None,
    ) -> Path:
        store = StateStore(self.state_root)
        task_dir = store.create_task_dir(task_id)
        store.write_prompt(task_dir, "manual")
        store.write_manifest(
            task_dir,
            {
                "schema_version": 2,
                "task_id": task_id,
                "status": "queued",
                "created_at": updated_at or utc_now(),
                "updated_at": updated_at or utc_now(),
                "workdir": str(self.workdir.resolve()),
                "route": {"sandbox": "read-only"},
                "timeout_sec": 15,
                "ownership_nonce_hash": "0" * 64,
                "supervisor_pid": None,
                "supervisor_start_marker": None,
                "runner_pid": None,
                "runner_start_marker": None,
                "exit_code": None,
                "completed_at": None,
                "error": None,
            },
        )
        self.task_ids.append(task_id)
        return task_dir

    def test_route_task_resolves_scoped_write(self) -> None:
        route = route_task(
            "implement parser",
            self.workdir,
            complexity="standard",
            intent="write",
            allowed_paths=["src/parser"],
        )
        self.assertEqual(route["sandbox"], "workspace-write")
        self.assertEqual(
            route["allowed_paths"],
            [str((self.workdir / "src/parser").resolve())],
        )

    def test_start_wait_and_result_complete_fake_task(self) -> None:
        started = self._start()
        self.assertEqual(started["schema_version"], 2)
        self.assertEqual(len(str(started["ownership_nonce_hash"])), 64)
        self.assertNotIn("ownership_nonce", started)
        waited = wait_task(
            str(started["task_id"]),
            self.state_root,
            wait_timeout_sec=20,
        )
        self.assertEqual(waited["status"], "completed")
        result = result_task(str(started["task_id"]), self.state_root)
        self.assertEqual(result["last_message"], "FAKE_RESULT_OK")

    def test_start_observes_supervisor_manifest_without_controller_overwrite(
        self,
    ) -> None:
        task_dir = self.state_root / "controller-observer"
        store = Mock()
        store.create_task_dir.return_value = task_dir
        observed = {
            "schema_version": 2,
            "task_id": task_dir.name,
            "status": "completed",
            "supervisor_pid": 321,
            "supervisor_start_marker": "supervisor-start",
            "runner_pid": 654,
            "runner_start_marker": "runner-start",
        }
        store.read_manifest.return_value = observed
        process = Mock(pid=321)
        process.wait.return_value = 0
        identity = ProcessIdentity(321, "supervisor-start", "owned")
        with patch.object(lifecycle, "_store", return_value=store), patch.object(
            lifecycle,
            "_launch_supervisor",
            return_value=process,
        ), patch.object(
            lifecycle,
            "_launched_identity",
            return_value=identity,
        ):
            started = start_task(
                prompt="observe",
                workdir=self.workdir,
                state_root=self.state_root,
                task_id=task_dir.name,
                engine="fake",
            )
        self.assertIs(started, observed)
        self.assertEqual(store.write_manifest.call_count, 1)
        initial_manifest = store.write_manifest.call_args.args[1]
        self.assertIs(initial_manifest["runner_launching"], False)

    def test_launched_identity_retries_transient_pre_exec_identity(self) -> None:
        process = Mock(pid=321)
        process.poll.return_value = None
        transient = ProcessIdentity(321, "start", "python parent.py")
        owned = ProcessIdentity(
            321,
            "start",
            "python -m codex_worker_dispatcher.supervisor owned",
        )
        task_dir = Path("state") / "worker-runs" / "owned-task"

        with patch.object(
            lifecycle,
            "read_process_identity",
            side_effect=(transient, owned),
        ) as read_identity, patch.object(
            lifecycle,
            "owned_task_identity_matches",
            side_effect=(False, True),
        ) as matches, patch.object(lifecycle.time, "sleep") as sleep:
            result = lifecycle._launched_identity(
                process,
                "ownership-nonce",
                task_dir,
            )

        self.assertIs(result, owned)
        self.assertEqual(read_identity.call_count, 2)
        self.assertEqual(matches.call_count, 2)
        sleep.assert_called_once_with(0.01)

    def test_launched_identity_bounds_persistent_identity_mismatch(self) -> None:
        process = Mock(pid=321)
        process.poll.return_value = None
        transient = ProcessIdentity(321, "start", "python parent.py")

        with patch.object(
            lifecycle,
            "read_process_identity",
            return_value=transient,
        ), patch.object(
            lifecycle,
            "owned_task_identity_matches",
            return_value=False,
        ), patch.object(
            lifecycle.time,
            "monotonic",
            side_effect=(0.0, 5.0),
        ), patch.object(lifecycle.time, "sleep") as sleep:
            with self.assertRaises(WorkerError) as raised:
                lifecycle._launched_identity(
                    process,
                    "ownership-nonce",
                    Path("state") / "worker-runs" / "owned-task",
                )

        self.assertEqual(raised.exception.code, "process_identity_mismatch")
        sleep.assert_not_called()

    def test_start_failure_after_runner_record_reclaims_every_owned_process(
        self,
    ) -> None:
        task_id = "20260718-110000-a1b2c3d4"
        self.task_ids.append(task_id)
        real_store = StateStore(self.state_root)

        class FaultStore:
            def create_task_dir(inner_self, value: str) -> Path:
                return real_store.create_task_dir(value)

            def write_prompt(inner_self, task_dir: Path, prompt: str) -> None:
                real_store.write_prompt(task_dir, prompt)

            def read_manifest(inner_self, task_dir: Path) -> dict[str, object]:
                deadline = time.monotonic() + 15
                while True:
                    current = real_store.read_manifest(task_dir)
                    if current.get("runner_pid") is not None:
                        raise RuntimeError("injected controller observation failure")
                    if time.monotonic() >= deadline:
                        raise AssertionError("runner identity was not recorded")
                    time.sleep(0.02)

            def write_manifest(
                inner_self,
                task_dir: Path,
                manifest: dict[str, object],
            ) -> None:
                real_store.write_manifest(task_dir, manifest)

        with patch.object(lifecycle, "_store", return_value=FaultStore()):
            with self.assertRaisesRegex(RuntimeError, "injected controller"):
                start_task(
                    prompt="long fake",
                    workdir=self.workdir,
                    state_root=self.state_root,
                    task_id=task_id,
                    engine="fake",
                    fake_delay_sec=60,
                    timeout_sec=120,
                )
        task_dir = real_store.task_dir(task_id)
        manifest = real_store.read_manifest(task_dir)
        runner_pid = manifest.get("runner_pid")
        supervisor_pid = manifest.get("supervisor_pid")
        self.assertIsInstance(runner_pid, int)
        self.assertIsInstance(supervisor_pid, int)
        self.assertTrue(wait_until_gone(int(runner_pid), 5.0))
        self.assertTrue(wait_until_gone(int(supervisor_pid), 5.0))

    def test_start_failure_waits_for_reserved_unrecorded_runner_cleanup(
        self,
    ) -> None:
        task_id = "20260718-110001-a1b2c3d4"
        nonce = "a" * 32
        real_store = StateStore(self.state_root)
        injection_dir = Path(self.temporary_directory.name) / "injection"
        injection_dir.mkdir()
        paused = injection_dir / "runner-paused"
        release = injection_dir / "runner-release"
        supervisor_stderr = injection_dir / "supervisor-stderr.log"
        injected_package = injection_dir / "codex_worker_dispatcher"
        injected_package.mkdir()
        real_package = Path(lifecycle.__file__).resolve().parent
        (injected_package / "__init__.py").write_text(
            f"__path__.append({str(real_package)!r})\n",
            encoding="utf-8",
        )
        (injected_package / "process.py").write_text(
            "import os,time\n"
            "import importlib.util,sys\n"
            "from pathlib import Path\n"
            f"spec=importlib.util.spec_from_file_location('codex_worker_dispatcher._real_process', {str(real_package / 'process.py')!r})\n"
            "real=importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name]=real\n"
            "spec.loader.exec_module(real)\n"
            "ProcessIdentity=real.ProcessIdentity\n"
            "owned_task_identity_matches=real.owned_task_identity_matches\n"
            "ownership_hash=real.ownership_hash\n"
            "terminate_owned_tree=real.terminate_owned_tree\n"
            "windows_detached_flags=real.windows_detached_flags\n"
            "original=real.read_process_identity\n"
            "paused=False\n"
            "def delayed(pid):\n"
            "  global paused\n"
            "  if pid != os.getpid() and not paused:\n"
            "    paused=True\n"
            "    Path(os.environ['CODEX_TEST_RUNNER_PAUSED']).write_text(str(pid), encoding='utf-8')\n"
            "    release=Path(os.environ['CODEX_TEST_RUNNER_RELEASE'])\n"
            "    deadline=time.monotonic()+30\n"
            "    while not release.exists():\n"
            "      assert time.monotonic() < deadline\n"
            "      time.sleep(0.02)\n"
            "  return original(pid)\n"
            "read_process_identity=delayed\n",
            encoding="utf-8",
        )
        observed_reservation: list[object] = []

        class FaultStore:
            def create_task_dir(inner_self, value: str) -> Path:
                return real_store.create_task_dir(value)

            def write_prompt(inner_self, task_dir: Path, prompt: str) -> None:
                real_store.write_prompt(task_dir, prompt)

            def read_manifest(inner_self, task_dir: Path) -> dict[str, object]:
                deadline = time.monotonic() + 15
                while not paused.exists():
                    if time.monotonic() >= deadline:
                        raise AssertionError(
                            "runner did not enter identity pause; "
                            f"stderr={supervisor_stderr.read_text(encoding='utf-8', errors='replace') if supervisor_stderr.exists() else ''}"
                        )
                    time.sleep(0.02)
                current = real_store.read_manifest(task_dir)
                if not observed_reservation:
                    observed_reservation.append(current.get("runner_launching"))
                raise RuntimeError("injected controller observation failure")

            def write_manifest(
                inner_self,
                task_dir: Path,
                manifest: dict[str, object],
            ) -> None:
                real_store.write_manifest(task_dir, manifest)

        def release_after_old_handshake() -> None:
            deadline = time.monotonic() + 20
            while not paused.exists():
                if time.monotonic() >= deadline:
                    return
                time.sleep(0.02)
            time.sleep(10.75)
            release.write_text("continue", encoding="utf-8")

        release_thread = threading.Thread(
            target=release_after_old_handshake,
            daemon=True,
        )
        release_thread.start()
        environment = {
            "PYTHONPATH": os.pathsep.join(
                filter(
                    None,
                    (
                        str(injection_dir),
                        os.environ.get("PYTHONPATH"),
                    ),
                )
            ),
            "CODEX_TEST_RUNNER_PAUSED": str(paused),
            "CODEX_TEST_RUNNER_RELEASE": str(release),
        }
        launch_environment = os.environ.copy()
        launch_environment.update(environment)

        def launch_supervisor(
            task_dir: Path,
            ownership_nonce: str,
        ) -> subprocess.Popen[bytes]:
            keyword_arguments: dict[str, object] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "close_fds": True,
                "env": launch_environment,
            }
            if os.name == "nt":
                keyword_arguments["creationflags"] = (
                    lifecycle.windows_detached_flags()
                )
            else:
                keyword_arguments["start_new_session"] = True
            with supervisor_stderr.open("wb") as stderr_stream:
                return subprocess.Popen(
                    lifecycle._supervisor_command(task_dir, ownership_nonce),
                    stderr=stderr_stream,
                    **keyword_arguments,
                )

        manifest: dict[str, object] = {}
        try:
            with patch.object(
                lifecycle,
                "_store",
                return_value=FaultStore(),
            ), patch.object(
                lifecycle,
                "_launch_supervisor",
                side_effect=launch_supervisor,
            ), patch.object(
                lifecycle.secrets,
                "token_hex",
                return_value=nonce,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected controller"):
                    start_task(
                        prompt="reserved fake",
                        workdir=self.workdir,
                        state_root=self.state_root,
                        task_id=task_id,
                        engine="fake",
                        fake_delay_sec=60,
                        timeout_sec=120,
                    )
            release_thread.join(timeout=20)
            self.assertFalse(release_thread.is_alive())
            self.assertTrue(release.exists())
            self.assertEqual(observed_reservation, [True])

            task_dir = real_store.task_dir(task_id)
            manifest = real_store.read_manifest(task_dir)
            runner_pid = manifest.get("runner_pid")
            supervisor_pid = manifest.get("supervisor_pid")
            self.assertIs(manifest.get("runner_launching"), False)
            self.assertIsInstance(runner_pid, int)
            self.assertIsInstance(supervisor_pid, int)
            self.assertTrue(wait_until_gone(int(runner_pid), 2.0))
            self.assertTrue(wait_until_gone(int(supervisor_pid), 2.0))
        finally:
            release.write_text("continue", encoding="utf-8")
            release_thread.join(timeout=20)
            task_dir = real_store.task_dir(task_id)
            try:
                manifest = real_store.read_manifest(task_dir)
            except WorkerError:
                manifest = {}
            pid_candidates = {
                "runner": manifest.get("runner_pid"),
                "supervisor": manifest.get("supervisor_pid"),
            }
            if paused.exists():
                pid_candidates["runner"] = int(
                    paused.read_text(encoding="utf-8")
                )
            for role, pid in pid_candidates.items():
                if not isinstance(pid, int):
                    continue
                try:
                    identity = read_process_identity(pid)
                except WorkerError:
                    continue
                if owned_task_identity_matches(
                    identity,
                    identity.start_marker,
                    nonce,
                    role,
                    task_dir,
                ):
                    try:
                        terminate_owned_tree(
                            pid,
                            identity.start_marker,
                            nonce,
                            1.0,
                        )
                    except WorkerError as error:
                        if error.code != "process_not_found":
                            raise

    def test_status_reconciles_completion_only_with_message_and_event(self) -> None:
        task_dir = self._manual_task("20260718-100000-a1b2c3d4")
        (task_dir / "last-message.txt").write_text("recovered", encoding="utf-8")
        (task_dir / "events.jsonl").write_text(
            json.dumps({"type": "turn.completed"}) + "\n",
            encoding="utf-8",
        )
        reconciled = status_task(task_dir.name, self.state_root)
        self.assertEqual(reconciled["status"], "completed")

    def test_status_marks_inactive_task_without_completion_evidence_orphaned(self) -> None:
        task_dir = self._manual_task("20260718-100001-a1b2c3d4")
        reconciled = status_task(task_dir.name, self.state_root)
        self.assertEqual(reconciled["status"], "orphaned")
        self.assertIs(reconciled["runner_launching"], False)
        self.assertIn("completion", str(reconciled["error"]).lower())

    def test_supervisor_terminal_write_wins_inactive_reconciliation_race(self) -> None:
        queued = {"task_id": "race-task", "status": "running"}
        cancelled = {"task_id": "race-task", "status": "cancelled"}
        store = Mock()
        store.read_manifest.return_value = cancelled
        with patch.object(
            lifecycle,
            "_task_has_live_process",
            return_value=False,
        ):
            reconciled = lifecycle._reconcile(
                store,
                Path("race-task"),
                queued,
            )
        self.assertIs(reconciled, cancelled)
        store.write_manifest.assert_not_called()

    def test_reconciliation_final_cas_preserves_injected_terminal_winner(
        self,
    ) -> None:
        task_dir = self._manual_task("20260718-100002-a1b2c3d4")
        (task_dir / "last-message.txt").write_text("partial", encoding="utf-8")
        store = StateStore(self.state_root)

        def inject_cancelled(_: Path) -> bool:
            def cancel(current: dict[str, object]) -> dict[str, object]:
                current.update(
                    {
                        "status": "cancelled",
                        "updated_at": "terminal-winner",
                        "completed_at": "terminal-winner",
                        "error": "injected terminal winner",
                    }
                )
                return current

            store.update_manifest(task_dir, cancel)
            return False

        initial = store.read_manifest(task_dir)
        with patch.object(
            lifecycle,
            "_task_has_live_process",
            return_value=False,
        ), patch.object(
            lifecycle,
            "_has_completion_event",
            side_effect=inject_cancelled,
        ):
            reconciled = lifecycle._reconcile(store, task_dir, initial)

        self.assertEqual(reconciled["status"], "cancelled")
        self.assertEqual(reconciled["updated_at"], "terminal-winner")
        self.assertEqual(store.read_manifest(task_dir), reconciled)

    def test_verified_live_process_prevents_orphan_reconciliation(self) -> None:
        running = {"task_id": "live-task", "status": "running"}
        store = Mock()
        with patch.object(
            lifecycle,
            "_task_has_live_process",
            return_value=True,
        ):
            reconciled = lifecycle._reconcile(
                store,
                Path("live-task"),
                running,
            )
        self.assertIs(reconciled, running)
        store.read_manifest.assert_not_called()
        store.write_manifest.assert_not_called()

    def test_identity_mismatch_with_completion_evidence_reconciles_completed(self) -> None:
        task_dir = self._manual_task("20260718-100004-a1b2c3d4")
        (task_dir / "last-message.txt").write_text("done\n", encoding="utf-8")
        (task_dir / "events.jsonl").write_text(
            json.dumps({"type": "fake.completed", "message": "done"}) + "\n",
            encoding="utf-8",
        )
        mismatch = WorkerError(
            "process_identity_mismatch",
            "recorded supervisor changed",
            {"pid": 22},
        )
        with patch.object(
            lifecycle,
            "_task_has_live_process",
            side_effect=mismatch,
        ):
            reconciled = status_task(task_dir.name, self.state_root)
        self.assertEqual(reconciled["status"], "completed")
        self.assertEqual(reconciled["exit_code"], 0)
        self.assertIsNone(reconciled["error"])

    def test_liveness_check_verifies_both_recorded_processes(self) -> None:
        identity = ProcessIdentity(11, "runner-start", "runner nonce")
        with patch.object(
            lifecycle,
            "_owned_process",
            side_effect=[(identity, "nonce"), None],
        ) as owned_process:
            self.assertTrue(
                lifecycle._task_has_live_process(
                    {"task_id": "live-task", "status": "running"},
                    Path("live-task"),
                )
            )
        self.assertEqual(
            [call.args[1] for call in owned_process.call_args_list],
            ["runner", "supervisor"],
        )

    def test_identity_mismatch_prevents_reconciliation_mutation(self) -> None:
        running = {"task_id": "mismatch-task", "status": "running"}
        store = Mock()
        mismatch = WorkerError(
            "process_identity_mismatch",
            "recorded process changed",
            {"pid": 22},
        )
        with patch.object(
            lifecycle,
            "_task_has_live_process",
            side_effect=mismatch,
        ), self.assertRaises(WorkerError) as raised:
            lifecycle._reconcile(store, Path("mismatch-task"), running)
        self.assertEqual(raised.exception.code, "process_identity_mismatch")
        store.read_manifest.assert_not_called()
        store.write_manifest.assert_not_called()

    def test_controller_wait_timeout_does_not_force_terminal_state(self) -> None:
        started = self._start(fake_delay_sec=5.0)
        with self.assertRaises(WorkerError) as raised:
            wait_task(
                str(started["task_id"]),
                self.state_root,
                wait_timeout_sec=0.05,
            )
        self.assertEqual(raised.exception.code, "wait_timeout")
        current = StateStore(self.state_root).read_manifest(
            StateStore(self.state_root).task_dir(str(started["task_id"]))
        )
        self.assertNotIn(current["status"], TERMINAL_STATES)

    def test_cancel_requests_cooperative_shutdown(self) -> None:
        started = self._start(fake_delay_sec=10)
        cancelled = cancel_task(
            str(started["task_id"]),
            self.state_root,
            wait_timeout_sec=20,
        )
        self.assertEqual(cancelled["status"], "cancelled")

    def test_reap_waits_for_reserved_runner_then_stops_runner_first(
        self,
    ) -> None:
        task_id = "20260718-110002-a1b2c3d4"
        nonce = "b" * 32
        self.task_ids.append(task_id)
        real_store = StateStore(self.state_root)
        injection_dir = Path(self.temporary_directory.name) / "reap-injection"
        injection_dir.mkdir()
        paused = injection_dir / "runner-paused"
        release = injection_dir / "runner-release"
        supervisor_stderr = injection_dir / "supervisor-stderr.log"
        injected_package = injection_dir / "codex_worker_dispatcher"
        injected_package.mkdir()
        real_package = Path(lifecycle.__file__).resolve().parent
        (injected_package / "__init__.py").write_text(
            f"__path__.append({str(real_package)!r})\n",
            encoding="utf-8",
        )
        (injected_package / "process.py").write_text(
            "import os,time\n"
            "import importlib.util,sys\n"
            "from pathlib import Path\n"
            f"spec=importlib.util.spec_from_file_location('codex_worker_dispatcher._real_process', {str(real_package / 'process.py')!r})\n"
            "real=importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name]=real\n"
            "spec.loader.exec_module(real)\n"
            "ProcessIdentity=real.ProcessIdentity\n"
            "owned_task_identity_matches=real.owned_task_identity_matches\n"
            "ownership_hash=real.ownership_hash\n"
            "terminate_owned_tree=real.terminate_owned_tree\n"
            "windows_detached_flags=real.windows_detached_flags\n"
            "windows_no_window_flags=real.windows_no_window_flags\n"
            "original=real.read_process_identity\n"
            "paused=False\n"
            "def delayed(pid):\n"
            "  global paused\n"
            "  if pid != os.getpid() and not paused:\n"
            "    paused=True\n"
            "    Path(os.environ['CODEX_TEST_RUNNER_PAUSED']).write_text(str(pid), encoding='utf-8')\n"
            "    release=Path(os.environ['CODEX_TEST_RUNNER_RELEASE'])\n"
            "    deadline=time.monotonic()+30\n"
            "    while not release.exists():\n"
            "      assert time.monotonic() < deadline\n"
            "      time.sleep(0.02)\n"
            "  return original(pid)\n"
            "read_process_identity=delayed\n",
            encoding="utf-8",
        )
        (injected_package / "supervisor.py").write_text(
            "from pathlib import Path\n"
            f"source=Path({str(real_package / 'supervisor.py')!r}).read_text(encoding='utf-8')\n"
            f"exec(compile(source, {str(real_package / 'supervisor.py')!r}, 'exec'), globals())\n",
            encoding="utf-8",
        )
        launch_environment = os.environ.copy()
        launch_environment.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    filter(
                        None,
                        (
                            str(injection_dir),
                            os.environ.get("PYTHONPATH"),
                        ),
                    )
                ),
                "CODEX_TEST_RUNNER_PAUSED": str(paused),
                "CODEX_TEST_RUNNER_RELEASE": str(release),
            }
        )

        def launch_supervisor(
            task_dir: Path,
            ownership_nonce: str,
        ) -> subprocess.Popen[bytes]:
            keyword_arguments: dict[str, object] = {
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "close_fds": True,
                "env": launch_environment,
            }
            if os.name == "nt":
                keyword_arguments["creationflags"] = (
                    lifecycle.windows_detached_flags()
                )
            else:
                keyword_arguments["start_new_session"] = True
            with supervisor_stderr.open("wb") as stderr_stream:
                return subprocess.Popen(
                    lifecycle._supervisor_command(task_dir, ownership_nonce),
                    stderr=stderr_stream,
                    **keyword_arguments,
                )

        reap_results: list[dict[str, object]] = []
        reap_errors: list[BaseException] = []
        termination_order: list[int] = []
        reaper: threading.Thread | None = None
        task_dir = real_store.task_dir(task_id)
        try:
            with patch.object(
                lifecycle,
                "_launch_supervisor",
                side_effect=launch_supervisor,
            ), patch.object(
                lifecycle.secrets,
                "token_hex",
                return_value=nonce,
            ):
                start_task(
                    prompt="reserved reap fake",
                    workdir=self.workdir,
                    state_root=self.state_root,
                    task_id=task_id,
                    engine="fake",
                    fake_delay_sec=60,
                    timeout_sec=120,
                )

            pause_deadline = time.monotonic() + 15
            while not paused.exists():
                if time.monotonic() >= pause_deadline:
                    stderr = (
                        supervisor_stderr.read_text(
                            encoding="utf-8",
                            errors="replace",
                        )
                        if supervisor_stderr.exists()
                        else ""
                    )
                    self.fail(f"runner did not enter identity pause; stderr={stderr}")
                time.sleep(0.02)

            paused_manifest = real_store.read_manifest(task_dir)
            runner_pid = int(paused.read_text(encoding="utf-8"))
            supervisor_pid = paused_manifest.get("supervisor_pid")
            self.assertIs(paused_manifest.get("runner_launching"), True)
            self.assertIsNone(paused_manifest.get("runner_pid"))
            self.assertIsNone(paused_manifest.get("runner_start_marker"))
            self.assertIsInstance(supervisor_pid, int)

            original_terminate = lifecycle.terminate_owned_tree

            def record_termination(
                pid: int,
                start_marker: str,
                ownership_nonce: str,
                grace_seconds: float,
            ) -> None:
                termination_order.append(pid)
                original_terminate(
                    pid,
                    start_marker,
                    ownership_nonce,
                    grace_seconds,
                )

            def reap_reserved_task() -> None:
                try:
                    reap_results.append(reap_task(task_id, self.state_root))
                except BaseException as error:
                    reap_errors.append(error)

            with patch.object(
                lifecycle,
                "terminate_owned_tree",
                side_effect=record_termination,
            ):
                reaper = threading.Thread(target=reap_reserved_task, daemon=True)
                reaper.start()
                time.sleep(16.0)

                self.assertTrue(
                    reaper.is_alive(),
                    "reap returned before the runner identity reservation cleared",
                )
                still_reserved = real_store.read_manifest(task_dir)
                self.assertIs(still_reserved.get("runner_launching"), True)
                self.assertIsNone(still_reserved.get("runner_pid"))
                self.assertIsNone(still_reserved.get("runner_start_marker"))
                read_process_identity(runner_pid)
                read_process_identity(int(supervisor_pid))

                release.write_text("continue", encoding="utf-8")
                reaper.join(timeout=20)
                self.assertFalse(reaper.is_alive())

            self.assertEqual(reap_errors, [])
            self.assertEqual(len(reap_results), 1)
            self.assertIn(reap_results[0].get("status"), TERMINAL_STATES)
            recorded = real_store.read_manifest(task_dir)
            self.assertEqual(recorded.get("runner_pid"), runner_pid)
            self.assertIs(recorded.get("runner_launching"), False)
            self.assertEqual(
                termination_order[:2],
                [runner_pid, int(supervisor_pid)],
            )
            self.assertTrue(wait_until_gone(runner_pid, 2.0))
            self.assertTrue(wait_until_gone(int(supervisor_pid), 2.0))
        finally:
            release.write_text("continue", encoding="utf-8")
            if reaper is not None:
                reaper.join(timeout=20)
            try:
                manifest = real_store.read_manifest(task_dir)
            except WorkerError:
                manifest = {}
            pid_candidates = {
                "runner": manifest.get("runner_pid"),
                "supervisor": manifest.get("supervisor_pid"),
            }
            if paused.exists():
                pid_candidates["runner"] = int(
                    paused.read_text(encoding="utf-8")
                )
            for role, pid in pid_candidates.items():
                if not isinstance(pid, int):
                    continue
                try:
                    identity = read_process_identity(pid)
                except WorkerError:
                    continue
                if owned_task_identity_matches(
                    identity,
                    identity.start_marker,
                    nonce,
                    role,
                    task_dir,
                ):
                    try:
                        terminate_owned_tree(
                            pid,
                            identity.start_marker,
                            nonce,
                            1.0,
                        )
                    except WorkerError as error:
                        if error.code != "process_not_found":
                            raise

    def test_reap_stops_owned_runner_before_supervisor(self) -> None:
        started = self._start(fake_delay_sec=10)
        reaped = reap_task(str(started["task_id"]), self.state_root)
        # The supervisor may persist failure after runner-first termination
        # before reap's terminal CAS; that terminal winner must remain immutable.
        self.assertIn(reaped["status"], {"reaped", "failed"})
        self.assertIs(reaped["runner_launching"], False)
        self.assertIsNotNone(reaped["completed_at"])

    def test_reap_clears_stale_runner_launch_reservation(self) -> None:
        task_dir = self._manual_task("20260718-100003-a1b2c3d4")
        store = StateStore(self.state_root)
        store.update_manifest(
            task_dir,
            lambda current: {**current, "runner_launching": True},
        )

        reaped = reap_task(task_dir.name, self.state_root)

        self.assertEqual(reaped["status"], "reaped")
        self.assertIs(reaped["runner_launching"], False)

    def test_reap_fails_closed_if_reserved_supervisor_has_exited(self) -> None:
        manifest = {
            "task_id": "crashed-reservation-task",
            "status": "running",
            "runner_launching": True,
            "runner_pid": None,
            "runner_start_marker": None,
            "supervisor_pid": 22,
            "supervisor_start_marker": "supervisor-start",
        }
        store = Mock()
        with patch.object(
            lifecycle,
            "_manifest_for_task",
            return_value=(store, Path("crashed-reservation-task"), manifest),
        ), patch.object(
            lifecycle,
            "_owned_process",
            return_value=None,
        ) as owned_process, patch.object(
            lifecycle,
            "terminate_owned_tree",
        ) as terminate, self.assertRaises(WorkerError) as raised:
            lifecycle.reap_task("crashed-reservation-task", self.state_root)

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(owned_process.call_count, 1)
        self.assertEqual(owned_process.call_args.args[1], "supervisor")
        terminate.assert_not_called()
        store.update_manifest.assert_not_called()

    def test_reap_reserved_runner_wait_is_bounded_without_busy_loop(self) -> None:
        manifest = {
            "task_id": "stuck-reservation-task",
            "status": "running",
            "runner_launching": True,
            "runner_pid": None,
            "runner_start_marker": None,
            "supervisor_pid": 22,
            "supervisor_start_marker": "supervisor-start",
        }
        store = Mock()
        store.read_manifest.return_value = dict(manifest)
        supervisor = ProcessIdentity(22, "supervisor-start", "supervisor nonce")

        class FakeTime:
            def __init__(inner_self) -> None:
                inner_self.current = 0.0
                inner_self.sleep_calls: list[float] = []

            def monotonic(inner_self) -> float:
                return inner_self.current

            def sleep(inner_self, seconds: float) -> None:
                inner_self.sleep_calls.append(seconds)
                inner_self.current += seconds

        fake_time = FakeTime()
        with patch.object(
            lifecycle,
            "_manifest_for_task",
            return_value=(store, Path("stuck-reservation-task"), manifest),
        ), patch.object(
            lifecycle,
            "_REAP_RESERVATION_HANDSHAKE_SECONDS",
            0.11,
            create=True,
        ), patch.object(
            lifecycle,
            "_owned_process",
            return_value=(supervisor, "supervisor-nonce"),
        ), patch.object(
            lifecycle,
            "time",
            fake_time,
        ), patch.object(
            lifecycle,
            "terminate_owned_tree",
        ) as terminate, self.assertRaises(WorkerError) as raised:
            lifecycle.reap_task("stuck-reservation-task", self.state_root)

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertGreaterEqual(len(fake_time.sleep_calls), 1)
        self.assertLessEqual(len(fake_time.sleep_calls), 4)
        terminate.assert_not_called()
        store.update_manifest.assert_not_called()

    def test_reap_preflights_both_identities_then_stops_runner_first(self) -> None:
        manifest = {"task_id": "ordered-task", "status": "running"}
        store = Mock()
        store.read_manifest.return_value = dict(manifest)
        runner = ProcessIdentity(11, "runner-start", "runner nonce")
        supervisor = ProcessIdentity(22, "supervisor-start", "supervisor nonce")
        with patch.object(
            lifecycle,
            "_manifest_for_task",
            return_value=(store, Path("ordered-task"), manifest),
        ), patch.object(
            lifecycle,
            "_owned_process",
            side_effect=[(runner, "runner-nonce"), (supervisor, "supervisor-nonce")],
        ) as owned_process, patch.object(
            lifecycle,
            "terminate_owned_tree",
        ) as terminate:
            lifecycle.reap_task("ordered-task", self.state_root)
        self.assertEqual(
            [call.args[1] for call in owned_process.call_args_list],
            ["runner", "supervisor"],
        )
        self.assertEqual(
            [call.args[0] for call in terminate.call_args_list],
            [11, 22],
        )

    def test_reap_identity_mismatch_signals_nothing_and_mutates_nothing(self) -> None:
        manifest = {"task_id": "mismatch-task", "status": "running"}
        store = Mock()
        mismatch = WorkerError(
            "process_identity_mismatch",
            "supervisor changed",
            {"pid": 22},
        )
        with patch.object(
            lifecycle,
            "_manifest_for_task",
            return_value=(store, Path("mismatch-task"), manifest),
        ), patch.object(
            lifecycle,
            "_owned_process",
            side_effect=[None, mismatch],
        ), patch.object(
            lifecycle,
            "terminate_owned_tree",
        ) as terminate, self.assertRaises(WorkerError) as raised:
            lifecycle.reap_task("mismatch-task", self.state_root)
        self.assertEqual(raised.exception.code, "process_identity_mismatch")
        terminate.assert_not_called()
        store.write_manifest.assert_not_called()

    def test_list_returns_sorted_tasks(self) -> None:
        first = self._start(task_id="20260718-100010-a1b2c3d4")
        second = self._start(task_id="20260718-100009-a1b2c3d4")
        wait_task(str(first["task_id"]), self.state_root, wait_timeout_sec=10)
        wait_task(str(second["task_id"]), self.state_root, wait_timeout_sec=10)
        listed = list_tasks(self.state_root)
        self.assertEqual(
            [task["task_id"] for task in listed["tasks"]],
            ["20260718-100009-a1b2c3d4", "20260718-100010-a1b2c3d4"],
        )

    def test_reap_stale_is_dry_run_until_apply(self) -> None:
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        task_dir = self._manual_task(
            "20260718-100020-a1b2c3d4",
            updated_at=old,
        )
        preview = reap_stale_tasks(
            self.state_root,
            older_than_sec=3600,
            apply=False,
        )
        self.assertEqual(preview["task_ids"], [task_dir.name])
        self.assertEqual(
            StateStore(self.state_root).read_manifest(task_dir)["status"],
            "queued",
        )
        applied = reap_stale_tasks(
            self.state_root,
            older_than_sec=3600,
            apply=True,
        )
        self.assertEqual(applied["task_ids"], [task_dir.name])
        self.assertEqual(
            StateStore(self.state_root).read_manifest(task_dir)["status"],
            "reaped",
        )

    def test_start_rejects_invalid_inputs_before_launch(self) -> None:
        for field, value in (
            ("prompt", ""),
            ("timeout_sec", float("inf")),
            ("task_id", "../escape"),
        ):
            with self.subTest(field=field), self.assertRaises(WorkerError):
                self._start(**{field: value})
        with self.assertRaises(WorkerError):
            self._start(workdir=self.workdir / "missing")


if __name__ == "__main__":
    unittest.main()
