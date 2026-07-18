from __future__ import annotations

import hashlib
import os
import re
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RELEASE_PLAN = (
    ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-07-17-cross-platform-codex-worker-dispatcher.md"
)
PUBLIC_DOCUMENTS = (
    ROOT / "README.md",
    ROOT / "README.zh-CN.md",
    ROOT / "SECURITY.md",
    ROOT / "CONTRIBUTING.md",
    ROOT / "LICENSE",
    ROOT / "AGENTS.md",
    ROOT / "PROJECT_STATUS.md",
    ROOT / "pyproject.toml",
)
FORBIDDEN_STATE_FILES = {
    ".env",
    "auth.json",
    "manifest.json",
    "prompt.txt",
    "events.jsonl",
    "stderr.log",
    "last-message.txt",
}
REMOVED_ALIASES = (
    "".join(("Lu", "na")),
    "".join(("Ter", "ra")),
    "".join(("gpt-5.6-", "lu", "na")),
)
AUDIT_IMPLEMENTATION_FILES: frozenset[Path] = frozenset()
SYNTHETIC_WINDOWS_PATHS = {
    Path("tests/test_process.py"): (
        "".join(("C", ":/Worker State/task-a")),
        "".join(("C", r":\\Program Files\\Python\\python.exe")),
        "".join(("C", r":\\Worker State\\task-a")),
    ),
    Path("tests/test_supervisor.py"): ("".join(("C", ":/Python/python.exe")),),
}


def _tracked_text_files() -> list[Path]:
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    relative_paths = (
        Path(value)
        for value in completed.stdout.decode("utf-8").split("\0")
        if value
    )
    paths: set[Path] = set(PUBLIC_DOCUMENTS)
    for relative in relative_paths:
        path = ROOT / relative
        if not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        paths.add(path)
    return sorted(paths)


def _without_synthetic_windows_paths(relative: Path, text: str) -> str:
    for value in SYNTHETIC_WINDOWS_PATHS.get(relative, ()):
        text = text.replace(value, "<synthetic-windows-path>")
    return text


class PublicReleaseAuditTests(unittest.TestCase):
    def _read_required(self, path: Path) -> str:
        self.assertTrue(path.is_file(), f"Required public file is missing: {path.name}")
        return path.read_text(encoding="utf-8")

    def test_distributable_text_has_no_private_machine_or_credential_data(self) -> None:
        patterns = {
            "local Windows path": re.compile(r"(?i)(?<![a-z0-9])[a-z]:[\\/]"),
            "local macOS home": re.compile("/" + r"Users/[^/\s]+/"),
            "local Linux home": re.compile("/" + r"home/[^/\s]+/"),
            "email address": re.compile(
                r"(?i)\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b"
            ),
            "API key assignment": re.compile(r"(?i)\bapi[_-]?key\s*="),
            "OpenAI-style token": re.compile(r"(?<![a-z0-9])sk-[a-z0-9_-]{20,}", re.I),
            "GitHub token": re.compile(
                r"(?<![a-z0-9])(?:ghp_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})",
                re.I,
            ),
            "GitLab token": re.compile(r"(?<![a-z0-9])glpat-[a-z0-9_-]{20,}", re.I),
            "AWS access key": re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}"),
        }
        for path in _tracked_text_files():
            text = self._read_required(path)
            relative = path.relative_to(ROOT)
            for label, pattern in patterns.items():
                audited_text = text
                if label == "local Windows path":
                    audited_text = _without_synthetic_windows_paths(relative, text)
                with self.subTest(path=relative, forbidden=label):
                    self.assertIsNone(
                        pattern.search(audited_text),
                        f"{relative} contains {label}",
                    )
            for alias in REMOVED_ALIASES:
                with self.subTest(path=relative, forbidden="removed model alias"):
                    self.assertNotIn(alias, text, f"{relative} contains a removed alias")

    def test_tracked_text_audit_has_no_file_level_test_exemptions(self) -> None:
        for relative in (
            Path("tests/test_public_release.py"),
            Path("tests/test_skill.py"),
        ):
            with self.subTest(path=relative):
                self.assertNotIn(relative, AUDIT_IMPLEMENTATION_FILES)

    def test_tracked_text_audit_covers_docs_and_test_fixtures(self) -> None:
        audited = {path.relative_to(ROOT) for path in _tracked_text_files()}
        for relative in (
            Path("docs/superpowers/plans/2026-07-17-cross-platform-codex-worker-dispatcher.md"),
            Path("docs/superpowers/specs/2026-07-17-cross-platform-codex-worker-dispatcher-design.md"),
            Path("tests/skill-evals/scenarios.md"),
            Path("tests/test_process.py"),
        ):
            with self.subTest(path=relative):
                self.assertIn(relative, audited)

    def test_repository_has_no_real_worker_state_or_auth_files(self) -> None:
        ignored_parts = {".git", ".venv", "build", ".pytest_cache", "__pycache__"}
        found = sorted(
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if path.is_file()
            and path.name in FORBIDDEN_STATE_FILES
            and ignored_parts.isdisjoint(path.relative_to(ROOT).parts)
        )

        self.assertEqual(found, [], f"Private runtime files must not be published: {found}")

    def test_english_readme_covers_installation_operation_and_release_status(self) -> None:
        text = self._read_required(ROOT / "README.md")
        headings = (
            "Prerequisites",
            "Installation",
            "Route preview",
            "Read-only example",
            "Scoped-write example",
            "Lifecycle and recovery",
            "State privacy",
            "Supported platforms",
            "Public release provenance",
            "Upgrade and uninstall",
            "Limitations",
            "License",
            "Unofficial project",
        )
        for heading in headings:
            with self.subTest(heading=heading):
                self.assertRegex(text, rf"(?im)^#+\s+{re.escape(heading)}\s*$")

        for snippet in (
            "Python 3.10+",
            "Codex CLI",
            "authenticated",
            "pipx install git+https://github.com/holdonyb/codex-worker-dispatcher.git",
            "python -m venv .venv",
            "codex-worker skill install",
            "codex-worker route",
            "--intent read",
            "--intent write",
            "--allowed-path",
            "codex-worker status",
            "codex-worker wait",
            "codex-worker result",
            "codex-worker cancel",
            "codex-worker reap",
            "codex-worker reap-stale",
            "--apply",
            "$CODEX_HOME/worker-runs",
            "~/.codex/worker-runs",
            "pipx upgrade codex-worker-dispatcher",
            "codex-worker skill install --upgrade",
            "codex-worker skill uninstall --yes",
            "pipx uninstall codex-worker-dispatcher",
            "Windows",
            "macOS",
            "Linux",
            "Apache-2.0",
            "Normal operational commands write one JSON object to stdout",
            "`--help` output is human-readable text",
            "Task 10 CI targets",
            "single-commit sanitized snapshot",
            "development history is never pushed",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, text)

    def test_chinese_readme_covers_installation_operation_and_release_status(self) -> None:
        text = self._read_required(ROOT / "README.zh-CN.md")
        headings = (
            "前置条件",
            "安装",
            "路由预览",
            "只读示例",
            "限定范围的写入示例",
            "生命周期与恢复",
            "状态文件与隐私",
            "支持的平台",
            "公开发布来源",
            "升级与卸载",
            "限制",
            "许可证",
            "非官方项目",
        )
        for heading in headings:
            with self.subTest(heading=heading):
                self.assertRegex(text, rf"(?m)^#+\s+{re.escape(heading)}\s*$")

        for snippet in (
            "Python 3.10+",
            "Codex CLI",
            "完成认证",
            "pipx install git+https://github.com/holdonyb/codex-worker-dispatcher.git",
            "python -m venv .venv",
            "codex-worker skill install",
            "codex-worker route",
            "--intent read",
            "--intent write",
            "--allowed-path",
            "codex-worker status",
            "codex-worker wait",
            "codex-worker result",
            "codex-worker cancel",
            "codex-worker reap",
            "codex-worker reap-stale",
            "--apply",
            "$CODEX_HOME/worker-runs",
            "~/.codex/worker-runs",
            "pipx upgrade codex-worker-dispatcher",
            "codex-worker skill install --upgrade",
            "codex-worker skill uninstall --yes",
            "pipx uninstall codex-worker-dispatcher",
            "Windows",
            "macOS",
            "Linux",
            "Apache-2.0",
            "正常操作命令只向 stdout 输出一个 JSON 对象",
            "`--help` 是供人阅读的文本",
            "Task 10 的 CI 目标",
            "单提交的净化快照",
            "开发历史不会被推送",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, text)

    def test_package_metadata_uses_the_public_readme(self) -> None:
        pyproject = self._read_required(ROOT / "pyproject.toml")
        self.assertIn('readme = "README.md"', pyproject)

    def test_license_has_the_apache_2_signature(self) -> None:
        text = self._read_required(ROOT / "LICENSE")
        self.assertTrue(
            text.startswith(
                "\n                                 Apache License\n"
                "                           Version 2.0, January 2004\n"
            )
        )
        self.assertIn("http://www.apache.org/licenses/", text)
        self.assertIn("TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION", text)
        self.assertIn("END OF TERMS AND CONDITIONS", text)
        self.assertEqual(
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
            "LICENSE must remain the unmodified Apache License 2.0 text",
        )

    def test_security_and_contribution_policies_cover_public_release_risks(self) -> None:
        security = self._read_required(ROOT / "SECURITY.md")
        self.assertIn("do not open a public issue", security.lower())
        self.assertIn("private vulnerability reporting", security.lower())
        self.assertIn("enabled and verified before the initial public release", security)

        contributing = self._read_required(ROOT / "CONTRIBUTING.md")
        for snippet in (
            "failing test first",
            "Windows, macOS, and Linux",
            "./.venv/Scripts/python -m unittest discover -s tests -v",
            "real prompts",
            "worker state",
            "Operational commands must write exactly one JSON value to stdout",
            "Argparse help remains human-readable text",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, contributing)

    def test_release_plan_publishes_only_a_verified_single_commit_snapshot(self) -> None:
        plan = self._read_required(RELEASE_PLAN)
        for snippet in (
            "git archive --format=tar -o",
            "git -C $snapshotRoot init -b main",
            'git -C $snapshotRoot commit -m "Release v0.1.0"',
            "git -C $snapshotRoot rev-list --count main",
            "& $snapshotPython -m unittest discover -s tests -v",
            "& $snapshotPython -m unittest tests.test_public_release tests.test_skill -v",
            "& $snapshotPython -m build",
            "git -C $snapshotRoot push -u origin main",
            "private-vulnerability-reporting",
            'gh api --method PUT repos/holdonyb/codex-worker-dispatcher/private-vulnerability-reporting',
            "--jq '.enabled'",
        ):
            with self.subTest(snippet=snippet):
                self.assertIn(snippet, plan)

        create_commands = tuple(
            line
            for line in plan.splitlines()
            if line.startswith("gh repo create holdonyb/codex-worker-dispatcher ")
        )
        self.assertEqual(len(create_commands), 1)
        self.assertNotIn("--source", create_commands[0])
        self.assertNotIn("--push", create_commands[0])


if __name__ == "__main__":
    unittest.main()
