from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


class ContinuousIntegrationContractTests(unittest.TestCase):
    def _workflow(self) -> str:
        self.assertTrue(WORKFLOW.is_file(), "The cross-platform CI workflow is missing")
        return WORKFLOW.read_text(encoding="utf-8")

    def test_workflow_covers_supported_platform_and_python_matrix(self) -> None:
        workflow = self._workflow()

        self.assertIn("actions/checkout@v4", workflow)
        self.assertIn("actions/setup-python@v5", workflow)
        self.assertRegex(
            workflow,
            r"os:\s*\[windows-latest, ubuntu-latest, macos-latest\]",
        )
        self.assertRegex(workflow, r"python-version:\s*\[\"3\.10\", \"3\.14\"\]")
        self.assertIn("fail-fast: false", workflow)

    def test_workflow_runs_tests_build_and_public_audit(self) -> None:
        workflow = self._workflow()

        for command in (
            "python -m pip install build",
            "python -m pip install -e .",
            "python -m unittest discover -s tests -v",
            "python -m build",
            "python -m unittest tests.test_public_release -v",
        ):
            with self.subTest(command=command):
                self.assertIn(command, workflow)

    def test_workflow_smoke_installs_wheel_offline_and_installs_skill(self) -> None:
        workflow = self._workflow()

        self.assertGreaterEqual(workflow.count("python -m venv"), 2)
        self.assertGreaterEqual(
            workflow.count(
                "-m pip install --no-index --find-links dist codex-worker-dispatcher"
            ),
            2,
        )
        self.assertGreaterEqual(workflow.count("--version"), 2)
        self.assertGreaterEqual(workflow.count("skill install --target"), 2)
        self.assertIn("runner.os == 'Windows'", workflow)
        self.assertIn("runner.os != 'Windows'", workflow)
        self.assertIn("shell: pwsh", workflow)
        self.assertIn("shell: bash", workflow)
        self.assertNotIn("Start-Process", workflow)

    def test_only_ubuntu_python_314_uploads_distributions(self) -> None:
        workflow = self._workflow()

        upload = re.search(
            r"(?ms)^\s*- name: Upload distributions\s+"
            r"if: (.+?)\s+uses: actions/upload-artifact@v4\s+with:\s+"
            r"name: distributions\s+path: dist/\*",
            workflow,
        )
        self.assertIsNotNone(upload, "The distribution upload step is missing")
        if upload is not None:
            condition = upload.group(1)
            self.assertIn("runner.os == 'Linux'", condition)
            self.assertIn("matrix.python-version == '3.14'", condition)


if __name__ == "__main__":
    unittest.main()
