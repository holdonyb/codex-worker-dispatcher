from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from codex_worker_dispatcher import cli
from codex_worker_dispatcher.lifecycle import reap_task
from codex_worker_dispatcher.process import windows_no_window_flags
from codex_worker_dispatcher.state import StateStore, TERMINAL_STATES


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name)
        self.workdir = root / "work"
        self.workdir.mkdir()
        self.state_root = root / "state"
        self.addCleanup(self._cleanup_tasks)

    def _run(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], object]:
        environment = os.environ.copy()
        package_root = str(Path(__file__).resolve().parents[1] / "src")
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(None, (package_root, environment.get("PYTHONPATH")))
        )
        completed = subprocess.run(
            [sys.executable, "-m", "codex_worker_dispatcher", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env=environment,
            timeout=30,
            creationflags=windows_no_window_flags(),
        )
        decoder = json.JSONDecoder()
        value, end = decoder.raw_decode(completed.stdout)
        self.assertFalse(completed.stdout[end:].strip(), completed.stdout)
        return completed, value

    def _cleanup_tasks(self) -> None:
        manifests = StateStore(self.state_root).list_manifests()
        for manifest in manifests:
            if manifest.get("status") not in TERMINAL_STATES:
                reaped = reap_task(str(manifest["task_id"]), self.state_root)
                self.assertIn(reaped.get("status"), TERMINAL_STATES)

    def test_route_outputs_one_success_object(self) -> None:
        completed, value = self._run(
            "route",
            "--prompt",
            "inspect parser",
            "--workdir",
            str(self.workdir),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(value["ok"])
        self.assertEqual(value["sandbox"], "read-only")

    def test_start_wait_and_result_black_box(self) -> None:
        completed, started = self._run(
            "start",
            "--prompt",
            "CLI_RESULT",
            "--workdir",
            str(self.workdir),
            "--state-root",
            str(self.state_root),
            "--engine",
            "fake",
            "--fake-delay-sec",
            "0.05",
            "--timeout-sec",
            "10",
        )
        start_diagnostics: dict[str, object] = {
            "stderr": completed.stderr,
            "response": started,
        }
        self.assertEqual(
            completed.returncode,
            0,
            json.dumps(start_diagnostics, ensure_ascii=False, indent=2),
        )
        task_id = started["task_id"]
        waited_process, waited = self._run(
            "wait",
            task_id,
            "--state-root",
            str(self.state_root),
            "--wait-timeout-sec",
            "15",
        )
        self.assertEqual(waited_process.returncode, 0, waited_process.stderr)
        if waited["status"] != "completed":
            store = StateStore(self.state_root)
            task_dir = store.task_dir(str(task_id))
            diagnostics: dict[str, object] = {
                "waited": waited,
                "manifest": store.read_manifest(task_dir),
                "files": sorted(path.name for path in task_dir.iterdir()),
            }
            for filename in ("stderr.log", "events.jsonl", "last-message.txt"):
                path = task_dir / filename
                if path.exists():
                    diagnostics[filename] = path.read_text(
                        encoding="utf-8",
                        errors="replace",
                    )
            self.fail(json.dumps(diagnostics, ensure_ascii=False, indent=2))
        result_process, result = self._run(
            "result",
            task_id,
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual(result_process.returncode, 0, result_process.stderr)
        self.assertEqual(result["last_message"], "CLI_RESULT")

    def test_invalid_task_id_is_worker_error_on_stdout(self) -> None:
        completed, value = self._run(
            "status",
            "../escape",
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(value["error"]["code"], "invalid_task_id")

    def test_unscoped_workspace_write_is_worker_error(self) -> None:
        completed, value = self._run(
            "route",
            "--prompt",
            "implement parser",
            "--workdir",
            str(self.workdir),
            "--intent",
            "write",
            "--sandbox",
            "workspace-write",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(value["error"]["code"], "write_not_authorized")

    def test_missing_task_is_worker_error(self) -> None:
        completed, value = self._run(
            "result",
            "missing-task",
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(value["error"]["code"], "task_not_found")

    def test_start_requires_exactly_one_prompt_source(self) -> None:
        completed, value = self._run(
            "start",
            "--workdir",
            str(self.workdir),
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(value["error"]["code"], "invalid_arguments")
        prompt_file = Path(self.temporary_directory.name) / "prompt.txt"
        prompt_file.write_text("from file", encoding="utf-8")
        both_process, both_value = self._run(
            "start",
            "--prompt",
            "inline",
            "--prompt-file",
            str(prompt_file),
            "--workdir",
            str(self.workdir),
            "--state-root",
            str(self.state_root),
        )
        self.assertEqual(both_process.returncode, 2)
        self.assertEqual(both_value["error"]["code"], "invalid_arguments")

    def test_unexpected_failure_is_one_internal_error_object(self) -> None:
        stdout = io.StringIO()
        with patch.object(
            cli,
            "_execute",
            side_effect=RuntimeError("boom"),
        ), redirect_stdout(stdout):
            exit_code = cli.main(
                [
                    "route",
                    "--prompt",
                    "inspect parser",
                    "--workdir",
                    str(self.workdir),
                ]
            )
        self.assertEqual(exit_code, 1)
        value = json.loads(stdout.getvalue())
        self.assertEqual(value["error"]["code"], "internal_error")


if __name__ == "__main__":
    unittest.main()
