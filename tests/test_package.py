import json
import subprocess
import sys
import unittest
from pathlib import Path

from codex_worker_dispatcher import __version__
from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.process import windows_no_window_flags


class PackageContractTests(unittest.TestCase):
    def test_skill_data_files_use_project_relative_source_paths(self) -> None:
        pyproject = (
            Path(__file__).resolve().parents[1] / "pyproject.toml"
        ).read_text(encoding="utf-8")

        for data_file_mapping in (
            '"share/codex-worker-dispatcher/skill/dispatching-codex-workers" = '
            '["skill/dispatching-codex-workers/SKILL.md"]',
            '"share/codex-worker-dispatcher/skill/dispatching-codex-workers/agents" = '
            '["skill/dispatching-codex-workers/agents/openai.yaml"]',
            '"share/codex-worker-dispatcher/skill/dispatching-codex-workers/references" = '
            '["skill/dispatching-codex-workers/references/design.md"]',
        ):
            with self.subTest(data_file_mapping=data_file_mapping):
                self.assertIn(data_file_mapping, pyproject)

    def test_version_is_public(self) -> None:
        self.assertEqual(__version__, "0.1.0")

    def test_worker_error_serializes_to_json_envelope(self) -> None:
        error = WorkerError(
            "invalid_arguments",
            "bad input",
            {"field": "prompt"},
        )

        self.assertEqual(
            error.to_dict(),
            {
                "ok": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": "bad input",
                    "details": {"field": "prompt"},
                },
            },
        )

    def test_worker_error_allows_exception_runtime_traceback_state(self) -> None:
        error = WorkerError("invalid_state", "broken", {"task_id": "task"})

        error.__traceback__ = None
        error.__cause__ = ValueError("cause")

        self.assertIsNone(error.__traceback__)
        self.assertIsInstance(error.__cause__, ValueError)
        with self.assertRaises(TypeError):
            error.details["task_id"] = "changed"  # type: ignore[index]

    def test_module_version_command_writes_json(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "codex_worker_dispatcher", "--version"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=windows_no_window_flags(),
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"version": "0.1.0"})


if __name__ == "__main__":
    unittest.main()
