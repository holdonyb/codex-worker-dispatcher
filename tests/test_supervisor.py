from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.process import (
    ProcessIdentity,
    ownership_hash,
    terminate_owned_tree,
    wait_until_gone,
    windows_no_window_flags,
)
from codex_worker_dispatcher.state import StateStore, utc_now


class SupervisorIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name).resolve(strict=False)
        self.state_root = self.base / "state" / "worker-runs"
        self.workdir = self.base / "workdir"
        self.workdir.mkdir()
        self.store = StateStore(self.state_root)
        self.processes: list[subprocess.Popen[str]] = []
        self.task_dirs: list[Path] = []
        self.addCleanup(self._cleanup_processes)

    def _create_fake_task(
        self,
        task_id: str,
        *,
        prompt: str = "FAKE_RESULT_OK",
        delay_seconds: float = 0.15,
        timeout_seconds: float = 5.0,
        exit_code: int = 0,
        nonce: str = "test-owner-nonce",
    ) -> tuple[Path, str]:
        task_dir = self.store.create_task_dir(task_id)
        self.task_dirs.append(task_dir)
        now = utc_now()
        self.store.write_prompt(task_dir, prompt)
        self.store.write_manifest(
            task_dir,
            {
                "schema_version": 2,
                "task_id": task_id,
                "status": "starting",
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "workdir": str(self.workdir.resolve()),
                "route": {
                    "sandbox": "read-only",
                    "model": None,
                    "reasoning": None,
                    "skip_git_repo_check": False,
                },
                "timeout_sec": timeout_seconds,
                "engine": "fake",
                "fake_delay_sec": delay_seconds,
                "fake_exit_code": exit_code,
                "ownership_nonce_hash": ownership_hash(nonce),
                "supervisor_pid": None,
                "supervisor_start_marker": None,
                "runner_pid": None,
                "runner_start_marker": None,
                "exit_code": None,
                "error": None,
            },
        )
        return task_dir, nonce

    def _start_supervisor(
        self,
        task_dir: Path,
        nonce: str,
    ) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "codex_worker_dispatcher.supervisor",
                "--task-dir",
                str(task_dir),
                "--ownership-nonce",
                nonce,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=windows_no_window_flags(),
        )
        self.processes.append(process)
        return process

    def _wait_for_runner(self, task_dir: Path, timeout: float = 5.0) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            manifest = self.store.read_manifest(task_dir)
            if manifest.get("runner_pid") and manifest.get("runner_start_marker"):
                return manifest
            time.sleep(0.02)
        self.fail("supervisor did not record the runner process")

    def _assert_artifacts(self, task_dir: Path) -> None:
        for filename in ("events.jsonl", "stderr.log", "last-message.txt"):
            with self.subTest(filename=filename):
                self.assertTrue((task_dir / filename).is_file())

    def _assert_terminal_fields(
        self,
        manifest: dict[str, object],
        expected_status: str,
    ) -> None:
        self.assertEqual(manifest["status"], expected_status)
        self.assertIsInstance(manifest["updated_at"], str)
        self.assertTrue(manifest["updated_at"])
        self.assertIsInstance(manifest["completed_at"], str)
        self.assertTrue(manifest["completed_at"])
        self.assertIsInstance(manifest["exit_code"], int)

    def _assert_runner_gone(self, manifest: dict[str, object]) -> None:
        runner_pid = manifest.get("runner_pid")
        self.assertIsInstance(runner_pid, int)
        self.assertTrue(wait_until_gone(runner_pid, 5.0))

    def _cleanup_processes(self) -> None:
        for task_dir in self.task_dirs:
            try:
                manifest = self.store.read_manifest(task_dir)
                pid = manifest.get("runner_pid")
                marker = manifest.get("runner_start_marker")
                nonce = "test-owner-nonce"
                if isinstance(pid, int) and isinstance(marker, str) and marker:
                    try:
                        if not wait_until_gone(pid, 0):
                            terminate_owned_tree(pid, marker, nonce, 0.2)
                    except WorkerError:
                        pass
            except (OSError, WorkerError):
                pass
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def test_fake_runner_completes_and_writes_observable_artifacts(self) -> None:
        prompt = "Return this exact fake result."
        task_dir, nonce = self._create_fake_task("complete-task", prompt=prompt)

        supervisor = self._start_supervisor(task_dir, nonce)
        stdout, stderr = supervisor.communicate(timeout=10)

        self.assertEqual(supervisor.returncode, 0, stderr)
        self.assertEqual(stdout, "")
        manifest = self.store.read_manifest(task_dir)
        self._assert_terminal_fields(manifest, "completed")
        self.assertEqual(manifest["exit_code"], 0)
        self.assertIsNone(manifest["error"])
        self._assert_runner_gone(manifest)
        self._assert_artifacts(task_dir)
        event_lines = (task_dir / "events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        events = [json.loads(line) for line in event_lines]
        self.assertEqual(events, [{"type": "fake.completed", "message": prompt}])
        self.assertEqual(
            (task_dir / "last-message.txt").read_text(encoding="utf-8"),
            prompt,
        )

    def test_nonzero_fake_exit_records_failure(self) -> None:
        task_dir, nonce = self._create_fake_task(
            "failed-task",
            prompt="expected failure",
            exit_code=7,
        )

        supervisor = self._start_supervisor(task_dir, nonce)
        _, stderr = supervisor.communicate(timeout=10)

        self.assertEqual(supervisor.returncode, 0, stderr)
        manifest = self.store.read_manifest(task_dir)
        self._assert_terminal_fields(manifest, "failed")
        self.assertEqual(manifest["exit_code"], 7)
        self.assertIn("7", str(manifest["error"]))
        self._assert_runner_gone(manifest)
        self._assert_artifacts(task_dir)
        event = json.loads((task_dir / "events.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(
            event,
            {"type": "fake.failed", "message": "expected failure"},
        )

    def test_cancel_request_reclaims_runner_and_records_cancelled(self) -> None:
        task_dir, nonce = self._create_fake_task(
            "cancel-task",
            delay_seconds=10.0,
        )
        supervisor = self._start_supervisor(task_dir, nonce)
        self._wait_for_runner(task_dir)

        (task_dir / "cancel.request").write_text("cancel\n", encoding="utf-8")
        _, stderr = supervisor.communicate(timeout=10)

        self.assertEqual(supervisor.returncode, 0, stderr)
        manifest = self.store.read_manifest(task_dir)
        self._assert_terminal_fields(manifest, "cancelled")
        self.assertIn("cancel", str(manifest["error"]).lower())
        self._assert_runner_gone(manifest)
        self._assert_artifacts(task_dir)

    def test_ttl_reclaims_runner_and_records_timed_out(self) -> None:
        task_dir, nonce = self._create_fake_task(
            "timeout-task",
            delay_seconds=10.0,
            timeout_seconds=0.25,
        )

        supervisor = self._start_supervisor(task_dir, nonce)
        _, stderr = supervisor.communicate(timeout=10)

        self.assertEqual(supervisor.returncode, 0, stderr)
        manifest = self.store.read_manifest(task_dir)
        self._assert_terminal_fields(manifest, "timed_out")
        self.assertIn("ttl", str(manifest["error"]).lower())
        self._assert_runner_gone(manifest)
        self._assert_artifacts(task_dir)

    def test_wrong_nonce_is_rejected_without_mutating_manifest(self) -> None:
        task_dir, _ = self._create_fake_task("nonce-task")
        manifest_path = task_dir / "manifest.json"
        original_manifest = manifest_path.read_bytes()

        supervisor = self._start_supervisor(task_dir, "wrong-nonce")
        _, stderr = supervisor.communicate(timeout=10)

        self.assertNotEqual(supervisor.returncode, 0)
        self.assertIn("nonce", stderr.lower())
        manifest = self.store.read_manifest(task_dir)
        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertEqual(manifest["status"], "starting")
        self.assertIsNone(manifest["completed_at"])
        self.assertIsNone(manifest["runner_pid"])

    def test_owned_supervisor_exception_records_failed(self) -> None:
        task_dir, nonce = self._create_fake_task(
            "owned-error-task",
            delay_seconds=10.0,
        )
        (task_dir / "cancel.request").mkdir()

        supervisor = self._start_supervisor(task_dir, nonce)
        _, stderr = supervisor.communicate(timeout=10)

        self.assertNotEqual(supervisor.returncode, 0)
        self.assertIn("cancellation request", stderr.lower())
        manifest = self.store.read_manifest(task_dir)
        self._assert_terminal_fields(manifest, "failed")
        self.assertIsInstance(manifest["supervisor_pid"], int)
        self.assertIsInstance(manifest["runner_pid"], int)
        self._assert_runner_gone(manifest)


class RealRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.base = Path(self.temporary_directory.name).resolve(strict=False)
        self.task_dir = self.base / "task"
        self.task_dir.mkdir()
        self.workdir = self.base / "work"
        self.workdir.mkdir()
        (self.task_dir / "prompt.txt").write_text("inspect this", encoding="utf-8")

    def _manifest(self) -> dict[str, object]:
        return {
            "engine": "codex",
            "workdir": str(self.workdir),
            "route": {
                "sandbox": "workspace-write",
                "model": "opaque-model",
                "reasoning": "high",
                "skip_git_repo_check": True,
            },
        }

    def test_identity_record_wait_covers_slow_windows_identity_queries(self) -> None:
        from codex_worker_dispatcher import runner

        nonce = "slow-identity-nonce"
        pending = {
            "task_id": self.task_dir.name,
            "status": "running",
            "ownership_nonce_hash": ownership_hash(nonce),
            "runner_launching": True,
            "runner_pid": None,
            "runner_start_marker": None,
        }
        recorded = {
            **pending,
            "runner_launching": False,
            "runner_pid": os.getpid(),
            "runner_start_marker": "runner-start",
        }
        store = Mock()
        store.read_manifest.side_effect = [pending, recorded]

        with patch.object(runner, "StateStore", return_value=store), patch.object(
            runner.time,
            "monotonic",
            side_effect=[0.0, 31.0],
        ), patch.object(runner.time, "sleep"):
            result = runner._wait_for_identity_record(self.task_dir, nonce)

        self.assertIs(result, recorded)

    def test_identity_record_wait_stops_when_launch_reservation_clears(self) -> None:
        from codex_worker_dispatcher import runner

        nonce = "cleared-reservation-nonce"
        manifest = {
            "task_id": self.task_dir.name,
            "status": "running",
            "ownership_nonce_hash": ownership_hash(nonce),
            "runner_launching": False,
            "runner_pid": None,
            "runner_start_marker": None,
        }
        store = Mock()
        store.read_manifest.return_value = manifest

        with patch.object(runner, "StateStore", return_value=store), patch.object(
            runner.time,
            "monotonic",
            side_effect=[0.0, 20.0],
        ), patch.object(runner.time, "sleep") as sleep, self.assertRaises(
            WorkerError
        ) as raised:
            runner._wait_for_identity_record(self.task_dir, nonce)

        self.assertIn("reservation", raised.exception.message.lower())
        sleep.assert_not_called()

    def test_identity_record_wait_stops_when_task_is_terminal(self) -> None:
        from codex_worker_dispatcher import runner

        nonce = "terminal-before-record-nonce"
        manifest = {
            "task_id": self.task_dir.name,
            "status": "failed",
            "ownership_nonce_hash": ownership_hash(nonce),
            "runner_launching": False,
            "runner_pid": os.getpid(),
            "runner_start_marker": "runner-start",
        }
        store = Mock()
        store.read_manifest.return_value = manifest

        with patch.object(runner, "StateStore", return_value=store), patch.object(
            runner.time,
            "monotonic",
            side_effect=[0.0, 20.0],
        ), patch.object(runner.time, "sleep") as sleep, self.assertRaises(
            WorkerError
        ) as raised:
            runner._wait_for_identity_record(self.task_dir, nonce)

        self.assertIn("terminal", raised.exception.message.lower())
        sleep.assert_not_called()

    def _native_launcher(self) -> Path:
        launcher = self.base / "codex.exe"
        launcher.write_bytes(b"fake native Codex executable")
        launcher.chmod(0o755)
        return launcher

    def _npm_shim_layout(
        self,
        kind: str,
        *,
        create_node: bool = True,
        create_script: bool = True,
    ) -> tuple[Path, Path, Path]:
        if kind == "global":
            shim_dir = self.base / "npm"
            script = (
                shim_dir
                / "node_modules"
                / "@openai"
                / "codex"
                / "bin"
                / "codex.js"
            )
        elif kind == "local":
            node_modules = self.base / "project" / "node_modules"
            shim_dir = node_modules / ".bin"
            script = node_modules / "@openai" / "codex" / "bin" / "codex.js"
        else:
            raise AssertionError(f"unexpected npm layout: {kind}")
        shim_dir.mkdir(parents=True)
        shim = shim_dir / "codex.cmd"
        shim.write_text("@echo off\r\nnode codex.js %*\r\n", encoding="utf-8")
        node = self.base / "node.exe"
        if create_node:
            node.write_bytes(b"fake native node executable")
            node.chmod(0o755)
        if create_script:
            script.parent.mkdir(parents=True)
            script.write_text("// fake Codex entry point\n", encoding="utf-8")
        return shim, node, script

    def _run_with_npm_layout(
        self,
        kind: str,
    ) -> tuple[list[str], Path, Path, str, Path]:
        from codex_worker_dispatcher import runner

        shim, node, script = self._npm_shim_layout(kind)
        special_root = Path(f"{self.base}&pipe|caret^")
        task_dir = special_root / "task"
        special_workdir = Path(f"{self.base / 'work'}&pipe|caret^")
        special_model = "model&pipe|caret^"
        manifest = self._manifest()
        manifest["workdir"] = str(special_workdir)
        route = dict(manifest["route"])  # type: ignore[arg-type]
        route["model"] = special_model
        manifest["route"] = route

        def resolve(name: str) -> str | None:
            if name == "codex":
                return str(shim)
            if name == "node":
                return str(node)
            return None

        with patch.object(runner.shutil, "which", side_effect=resolve), patch.object(
            runner.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run, patch.object(
            runner.Path,
            "is_dir",
            return_value=True,
        ), patch.object(
            runner,
            "_read_prompt",
            return_value="inspect this",
        ), patch.object(
            runner,
            "_create_output",
            side_effect=lambda _: io.StringIO(),
        ):
            runner.run_task(task_dir, manifest)

        self.assertNotIn("shell", run.call_args.kwargs)
        return run.call_args.args[0], node, script, special_model, task_dir

    def _assert_safe_npm_invocation(self, kind: str) -> None:
        arguments, node, script, special_model, task_dir = self._run_with_npm_layout(
            kind
        )
        self.assertEqual(arguments[0], str(node.resolve()))
        self.assertEqual(arguments[1], str(script.resolve()))
        self.assertEqual(arguments[arguments.index("--model") + 1], special_model)
        self.assertIn("&pipe|caret^", arguments[arguments.index("-C") + 1])
        self.assertIn("&pipe|caret^", arguments[arguments.index("-o") + 1])
        self.assertEqual(
            arguments[arguments.index("-o") + 1],
            str(task_dir / "last-message.txt"),
        )
        self.assertFalse(
            any(
                argument.lower().endswith((".cmd", ".bat"))
                for argument in arguments
            )
        )
        self.assertNotIn("cmd", [argument.lower() for argument in arguments])
        self.assertNotIn("call", [argument.lower() for argument in arguments])

    def test_real_runner_builds_approved_argument_array(self) -> None:
        from codex_worker_dispatcher import runner

        completed = subprocess.CompletedProcess([], 0)
        launcher = self._native_launcher()
        with patch.object(
            runner.shutil,
            "which",
            return_value=str(launcher),
        ), patch.object(runner.subprocess, "run", return_value=completed) as run:
            exit_code = runner.run_task(self.task_dir, self._manifest())

        self.assertEqual(exit_code, 0)
        arguments = run.call_args.args[0]
        self.assertEqual(
            arguments[0:5],
            [str(launcher.resolve()), "exec", "--ephemeral", "--json", "--color"],
        )
        self.assertIn("never", arguments)
        self.assertIn("--sandbox", arguments)
        self.assertIn("workspace-write", arguments)
        self.assertIn("--model", arguments)
        self.assertIn("opaque-model", arguments)
        self.assertIn("model_reasoning_effort=\"high\"", arguments)
        self.assertIn("--skip-git-repo-check", arguments)
        self.assertEqual(arguments[-1], "-")
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["input"], "inspect this")
        self.assertEqual(run.call_args.kwargs["cwd"], self.workdir)
        expected_flags = (
            subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )
        self.assertEqual(
            run.call_args.kwargs.get("creationflags"),
            expected_flags,
        )

    def test_global_npm_shim_uses_native_node_and_regular_codex_script(self) -> None:
        self._assert_safe_npm_invocation("global")

    def test_local_npm_shim_uses_native_node_and_regular_codex_script(self) -> None:
        self._assert_safe_npm_invocation("local")

    def test_npm_shim_without_native_node_fails_closed(self) -> None:
        from codex_worker_dispatcher import runner

        shim, _, _ = self._npm_shim_layout("global", create_node=False)

        def resolve(name: str) -> str | None:
            return str(shim) if name == "codex" else None

        with patch.object(runner.shutil, "which", side_effect=resolve), patch.object(
            runner.subprocess,
            "run",
        ) as run, self.assertRaises(WorkerError) as raised:
            runner.run_task(self.task_dir, self._manifest())

        self.assertEqual(raised.exception.code, "codex_not_found")
        self.assertIn("launcher", raised.exception.message.lower())
        self.assertEqual(raised.exception.details["launcher"], str(shim.resolve()))
        run.assert_not_called()

    def test_npm_shim_without_codex_script_fails_closed(self) -> None:
        from codex_worker_dispatcher import runner

        shim, node, _ = self._npm_shim_layout("local", create_script=False)

        def resolve(name: str) -> str | None:
            if name == "codex":
                return str(shim)
            if name == "node":
                return str(node)
            return None

        with patch.object(runner.shutil, "which", side_effect=resolve), patch.object(
            runner.subprocess,
            "run",
        ) as run, self.assertRaises(WorkerError) as raised:
            runner.run_task(self.task_dir, self._manifest())

        self.assertEqual(raised.exception.code, "codex_not_found")
        self.assertIn("launcher", raised.exception.message.lower())
        self.assertEqual(raised.exception.details["launcher"], str(shim.resolve()))
        run.assert_not_called()

    def test_npm_shim_rejects_batch_node_launcher(self) -> None:
        from codex_worker_dispatcher import runner

        shim, _, _ = self._npm_shim_layout("global", create_node=False)
        node = self.base / "node.cmd"
        node.write_text("@echo off\r\n", encoding="utf-8")

        def resolve(name: str) -> str | None:
            if name == "codex":
                return str(shim)
            if name == "node":
                return str(node)
            return None

        with patch.object(runner.shutil, "which", side_effect=resolve), patch.object(
            runner.subprocess,
            "run",
        ) as run, self.assertRaises(WorkerError) as raised:
            runner.run_task(self.task_dir, self._manifest())

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertIn("non-native", raised.exception.message.lower())
        run.assert_not_called()

    def test_windows_powershell_launcher_fails_closed(self) -> None:
        from codex_worker_dispatcher import runner

        launcher = self.base / "codex.ps1"
        launcher.write_text("Write-Output unsafe\n", encoding="utf-8")
        with patch.object(runner.sys, "platform", "win32"), patch.object(
            runner.shutil,
            "which",
            return_value=str(launcher),
        ), patch.object(runner.subprocess, "run") as run, self.assertRaises(
            WorkerError
        ) as raised:
            runner.run_task(self.task_dir, self._manifest())

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertIn("launcher", raised.exception.message.lower())
        run.assert_not_called()

    def test_windows_extensionless_launcher_fails_closed(self) -> None:
        from codex_worker_dispatcher import runner

        launcher = self.base / "codex"
        launcher.write_text("unsafe script\n", encoding="utf-8")
        with patch.object(runner.sys, "platform", "win32"), patch.object(
            runner.shutil,
            "which",
            return_value=str(launcher),
        ), patch.object(runner.subprocess, "run") as run, self.assertRaises(
            WorkerError
        ) as raised:
            runner.run_task(self.task_dir, self._manifest())

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertIn("launcher", raised.exception.message.lower())
        run.assert_not_called()

    def test_windows_missing_native_launcher_fails_closed(self) -> None:
        from codex_worker_dispatcher import runner

        launcher = self.base / "missing-codex.exe"
        with patch.object(runner.sys, "platform", "win32"), patch.object(
            runner.shutil,
            "which",
            return_value=str(launcher),
        ), patch.object(runner.subprocess, "run") as run, self.assertRaises(
            WorkerError
        ) as raised:
            runner.run_task(self.task_dir, self._manifest())

        self.assertEqual(raised.exception.code, "codex_not_found")
        self.assertEqual(raised.exception.details["launcher"], str(launcher))
        run.assert_not_called()

    def test_windows_native_launcher_resolution_race_fails_closed(self) -> None:
        from codex_worker_dispatcher import runner

        launcher = self._native_launcher()
        with patch.object(runner.sys, "platform", "win32"), patch.object(
            runner.shutil,
            "which",
            return_value=str(launcher),
        ), patch.object(
            runner.Path,
            "resolve",
            side_effect=FileNotFoundError("launcher disappeared"),
        ), self.assertRaises(WorkerError) as raised:
            runner._resolve_codex_command()

        self.assertEqual(raised.exception.code, "codex_not_found")
        self.assertEqual(raised.exception.details["launcher"], str(launcher))

    def test_real_runner_omits_optional_overrides(self) -> None:
        from codex_worker_dispatcher import runner

        manifest = self._manifest()
        manifest["route"] = {
            "sandbox": "read-only",
            "model": None,
            "reasoning": None,
            "skip_git_repo_check": False,
        }
        launcher = self._native_launcher()
        with patch.object(
            runner.shutil,
            "which",
            return_value=str(launcher),
        ), patch.object(
            runner.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            runner.run_task(self.task_dir, manifest)

        arguments = run.call_args.args[0]
        self.assertNotIn("--model", arguments)
        self.assertNotIn("-c", arguments)
        self.assertNotIn("--skip-git-repo-check", arguments)

    def test_real_runner_reports_missing_codex_before_launch(self) -> None:
        from codex_worker_dispatcher import runner

        with patch.object(runner.shutil, "which", return_value=None), patch.object(
            runner.subprocess,
            "run",
        ) as run, self.assertRaises(WorkerError) as raised:
            runner.run_task(self.task_dir, self._manifest())

        self.assertEqual(raised.exception.code, "codex_not_found")
        run.assert_not_called()
        self.assertTrue((self.task_dir / "events.jsonl").is_file())
        self.assertTrue((self.task_dir / "last-message.txt").is_file())
        self.assertIn(
            "codex_not_found",
            (self.task_dir / "stderr.log").read_text(encoding="utf-8"),
        )

    def test_real_runner_accepts_top_level_skip_git_request(self) -> None:
        from codex_worker_dispatcher import runner

        manifest = self._manifest()
        route = dict(manifest["route"])  # type: ignore[arg-type]
        route["skip_git_repo_check"] = False
        manifest["route"] = route
        manifest["skip_git_repo_check"] = True
        launcher = self._native_launcher()
        with patch.object(
            runner.shutil,
            "which",
            return_value=str(launcher),
        ), patch.object(
            runner.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0),
        ) as run:
            runner.run_task(self.task_dir, manifest)

        self.assertIn("--skip-git-repo-check", run.call_args.args[0])


class SupervisorLaunchTests(unittest.TestCase):
    def _create_supervision_task(
        self,
        base: Path,
        *,
        task_id: str,
        nonce: str,
        timeout_seconds: float,
        cancel: bool,
    ) -> tuple[StateStore, Path]:
        store = StateStore(base / "state" / "worker-runs")
        task_dir = store.create_task_dir(task_id)
        now = utc_now()
        store.write_manifest(
            task_dir,
            {
                "task_id": task_id,
                "status": "starting",
                "updated_at": now,
                "started_at": None,
                "completed_at": None,
                "timeout_sec": timeout_seconds,
                "ownership_nonce_hash": ownership_hash(nonce),
                "runner_pid": None,
                "runner_start_marker": None,
                "exit_code": None,
                "error": None,
            },
        )
        if cancel:
            (task_dir / "cancel.request").write_text("cancel\n", encoding="utf-8")
        return store, task_dir

    def _supervise_with_mock_runner(
        self,
        task_dir: Path,
        nonce: str,
        runner_process: Mock,
        *,
        artifacts_ready: bool,
        termination_error: WorkerError | None = None,
        forbidden_sleep: str | None = None,
    ) -> str:
        from codex_worker_dispatcher import supervisor

        supervisor_identity = ProcessIdentity(10, "supervisor-start", nonce)
        runner_identity = ProcessIdentity(runner_process.pid, "runner-start", nonce)
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(
                    supervisor,
                    "_current_owned_identity",
                    return_value=supervisor_identity,
                )
            )
            stack.enter_context(
                patch.object(
                    supervisor,
                    "_launch_runner",
                    return_value=runner_process,
                )
            )
            stack.enter_context(
                patch.object(
                    supervisor,
                    "_read_owned_runner_identity",
                    return_value=runner_identity,
                )
            )
            stack.enter_context(
                patch.object(
                    supervisor,
                    "_runner_artifacts_ready",
                    return_value=artifacts_ready,
                )
            )
            stack.enter_context(
                patch.object(
                    supervisor,
                    "terminate_owned_tree",
                    side_effect=termination_error,
                )
            )
            if forbidden_sleep is not None:
                stack.enter_context(
                    patch.object(
                        supervisor.time,
                        "sleep",
                        side_effect=AssertionError(forbidden_sleep),
                    )
                )
            return supervisor.supervise(task_dir, nonce)

    def test_launch_preserves_environment_and_uses_package_parent(self) -> None:
        from codex_worker_dispatcher import supervisor

        task_dir = Path("state") / "worker-runs" / "launch-task"
        nonce = "exact-launch-nonce"
        base_executable = "C:/Python/python.exe"
        package_path = Path(supervisor.__file__).resolve()
        with patch.object(supervisor, "Path", return_value=package_path), patch.object(
            supervisor.os, "name", "nt"
        ), patch.object(
            supervisor.sys,
            "_base_executable",
            base_executable,
            create=True,
        ), patch.dict(
            supervisor.os.environ,
            {"PYTHONPATH": "existing-package-path"},
            clear=True,
        ), patch.object(supervisor.subprocess, "Popen") as popen:
            supervisor._launch_runner(task_dir, nonce)

        arguments = popen.call_args.args[0]
        environment = popen.call_args.kwargs["env"]
        package_parent = str(package_path.parents[1])
        self.assertEqual(arguments[0], base_executable)
        self.assertIn(nonce, arguments)
        self.assertEqual(arguments.count(nonce), 1)
        self.assertEqual(
            environment["PYTHONPATH"].split(os.pathsep),
            [package_parent, "existing-package-path"],
        )
        expected_flags = (
            subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            if sys.platform == "win32"
            else 0
        )
        self.assertEqual(
            popen.call_args.kwargs.get("creationflags"),
            expected_flags,
        )

    def test_identity_recording_failure_still_reclaims_verified_runner(self) -> None:
        from codex_worker_dispatcher import supervisor

        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            store = StateStore(base / "state" / "worker-runs")
            task_dir = store.create_task_dir("cleanup-task")
            nonce = "cleanup-nonce"
            now = utc_now()
            store.write_manifest(
                task_dir,
                {
                    "task_id": "cleanup-task",
                    "status": "starting",
                    "updated_at": now,
                    "started_at": None,
                    "timeout_sec": 5.0,
                    "ownership_nonce_hash": ownership_hash(nonce),
                },
            )
            supervisor_identity = ProcessIdentity(10, "supervisor-start", nonce)
            runner_identity = ProcessIdentity(20, "runner-start", nonce)
            runner_process = Mock(pid=20)
            recording_error = WorkerError(
                "invalid_state",
                "identity query failed",
                {"pid": 20},
            )
            with patch.object(
                supervisor,
                "_current_owned_identity",
                return_value=supervisor_identity,
            ), patch.object(
                supervisor,
                "_launch_runner",
                return_value=runner_process,
            ), patch.object(
                supervisor,
                "_read_owned_runner_identity",
                side_effect=recording_error,
            ), patch.object(
                supervisor,
                "read_process_identity",
                return_value=runner_identity,
            ), patch.object(
                supervisor,
                "owned_task_identity_matches",
                return_value=True,
            ), patch.object(supervisor, "_stop_runner") as stop_runner:
                with self.assertRaises(WorkerError) as raised:
                    supervisor.supervise(task_dir, nonce)

            self.assertIs(raised.exception, recording_error)
            stop_runner.assert_called_once_with(
                runner_process,
                runner_identity,
                nonce,
            )

    def test_failure_recording_refuses_unbound_supervisor_identity(self) -> None:
        from codex_worker_dispatcher import supervisor

        with tempfile.TemporaryDirectory() as temporary_directory:
            nonce = "failure-identity-nonce"
            store, task_dir = self._create_supervision_task(
                Path(temporary_directory),
                task_id="failure-identity-task",
                nonce=nonce,
                timeout_seconds=5.0,
                cancel=False,
            )
            before = store.read_manifest(task_dir)
            mismatch = WorkerError(
                "process_identity_mismatch",
                "supervisor role changed",
                {"pid": 10},
            )
            with patch.object(
                supervisor,
                "_current_owned_identity",
                side_effect=mismatch,
            ):
                supervisor._record_failure(task_dir, nonce, mismatch)

            self.assertEqual(store.read_manifest(task_dir), before)

    def test_runner_launch_failure_clears_reservation_in_terminal_manifest(
        self,
    ) -> None:
        from codex_worker_dispatcher import supervisor

        with tempfile.TemporaryDirectory() as temporary_directory:
            nonce = "launch-failure-nonce"
            store, task_dir = self._create_supervision_task(
                Path(temporary_directory),
                task_id="launch-failure-task",
                nonce=nonce,
                timeout_seconds=5.0,
                cancel=False,
            )
            supervisor_identity = ProcessIdentity(10, "supervisor-start", nonce)
            launch_error = WorkerError(
                "invalid_state",
                "injected runner launch failure",
                {"task_id": task_dir.name},
            )
            with patch.object(
                supervisor,
                "_current_owned_identity",
                return_value=supervisor_identity,
            ), patch.object(
                supervisor,
                "_launch_runner",
                side_effect=launch_error,
            ):
                with self.assertRaises(WorkerError) as raised:
                    supervisor.supervise(task_dir, nonce)

            self.assertIs(raised.exception, launch_error)
            manifest = store.read_manifest(task_dir)
            self.assertEqual(manifest["status"], "failed")
            self.assertIs(manifest["runner_launching"], False)

    def test_cancel_wins_when_runner_exits_zero_before_termination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            nonce = "cancel-race-nonce"
            store, task_dir = self._create_supervision_task(
                Path(temporary_directory),
                task_id="cancel-race-task",
                nonce=nonce,
                timeout_seconds=5.0,
                cancel=True,
            )
            runner_process = Mock(pid=20)
            runner_process.poll.side_effect = [None, None, 0]
            termination_race = WorkerError(
                "process_not_found",
                "runner exited",
                {"pid": 20},
            )
            status = self._supervise_with_mock_runner(
                task_dir,
                nonce,
                runner_process,
                artifacts_ready=True,
                termination_error=termination_race,
            )

            manifest = store.read_manifest(task_dir)
            self.assertEqual(status, "cancelled")
            self.assertEqual(manifest["status"], "cancelled")
            self.assertEqual(manifest["exit_code"], 0)

    def test_ttl_wins_when_runner_exits_nonzero_before_termination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            nonce = "ttl-race-nonce"
            store, task_dir = self._create_supervision_task(
                Path(temporary_directory),
                task_id="ttl-race-task",
                nonce=nonce,
                timeout_seconds=0.0,
                cancel=False,
            )
            runner_process = Mock(pid=20)
            runner_process.poll.side_effect = [None, None, 7]
            termination_race = WorkerError(
                "process_identity_mismatch",
                "runner changed while stopping",
                {"pid": 20},
            )
            status = self._supervise_with_mock_runner(
                task_dir,
                nonce,
                runner_process,
                artifacts_ready=True,
                termination_error=termination_race,
            )

            manifest = store.read_manifest(task_dir)
            self.assertEqual(status, "timed_out")
            self.assertEqual(manifest["status"], "timed_out")
            self.assertEqual(manifest["exit_code"], 7)

    def test_cancel_does_not_wait_for_runner_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            nonce = "cancel-artifacts-nonce"
            store, task_dir = self._create_supervision_task(
                Path(temporary_directory),
                task_id="cancel-artifacts-task",
                nonce=nonce,
                timeout_seconds=5.0,
                cancel=True,
            )
            runner_process = Mock(pid=20)
            runner_process.poll.side_effect = [None, None]
            runner_process.wait.return_value = -15
            status = self._supervise_with_mock_runner(
                task_dir,
                nonce,
                runner_process,
                artifacts_ready=False,
                forbidden_sleep="cancellation was delayed",
            )

            manifest = store.read_manifest(task_dir)
            self.assertEqual(status, "cancelled")
            self.assertEqual(manifest["status"], "cancelled")

    def test_ttl_does_not_wait_for_runner_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            nonce = "ttl-artifacts-nonce"
            store, task_dir = self._create_supervision_task(
                Path(temporary_directory),
                task_id="ttl-artifacts-task",
                nonce=nonce,
                timeout_seconds=0.0,
                cancel=False,
            )
            runner_process = Mock(pid=20)
            runner_process.poll.side_effect = [None, None]
            runner_process.wait.return_value = -9
            status = self._supervise_with_mock_runner(
                task_dir,
                nonce,
                runner_process,
                artifacts_ready=False,
                forbidden_sleep="TTL handling was delayed",
            )

            manifest = store.read_manifest(task_dir)
            self.assertEqual(status, "timed_out")
            self.assertEqual(manifest["status"], "timed_out")

    def test_runner_exit_without_artifacts_records_useful_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            nonce = "missing-artifacts-nonce"
            store, task_dir = self._create_supervision_task(
                Path(temporary_directory),
                task_id="missing-artifacts-task",
                nonce=nonce,
                timeout_seconds=5.0,
                cancel=False,
            )
            runner_process = Mock(pid=20)
            runner_process.poll.return_value = 3
            status = self._supervise_with_mock_runner(
                task_dir,
                nonce,
                runner_process,
                artifacts_ready=False,
            )

            manifest = store.read_manifest(task_dir)
            self.assertEqual(status, "failed")
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["exit_code"], 3)
            self.assertIn("observable outputs", str(manifest["error"]))

    def test_stop_race_fails_closed_while_runner_is_still_live(self) -> None:
        from codex_worker_dispatcher import supervisor

        nonce = "live-race-nonce"
        identity = ProcessIdentity(20, "runner-start", nonce)
        runner_process = Mock(pid=20)
        runner_process.poll.side_effect = [None, None]
        runner_process.wait.side_effect = subprocess.TimeoutExpired(
            ["python", "runner.py"],
            0,
        )
        termination_error = WorkerError(
            "process_identity_mismatch",
            "runner identity changed",
            {"pid": 20},
        )
        with patch.object(
            supervisor,
            "terminate_owned_tree",
            side_effect=termination_error,
        ), self.assertRaises(WorkerError) as raised:
            supervisor._stop_runner(runner_process, identity, nonce)

        self.assertIs(raised.exception, termination_error)


if __name__ == "__main__":
    unittest.main()
