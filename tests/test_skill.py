import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skill" / "dispatching-codex-workers"
SKILL_PATH = SKILL_DIR / "SKILL.md"
DESIGN_PATH = SKILL_DIR / "references" / "design.md"
OPENAI_PATH = SKILL_DIR / "agents" / "openai.yaml"


class PublicSkillTests(unittest.TestCase):
    def _read_required(self, path: Path) -> str:
        self.assertTrue(path.is_file(), f"Required public skill file is missing: {path}")
        return path.read_text(encoding="utf-8")

    def _skill_parts(self) -> tuple[dict[str, str], str]:
        text = self._read_required(SKILL_PATH)
        match = re.fullmatch(
            r"---\r?\n(?P<frontmatter>.*?)\r?\n---\r?\n(?P<body>.*)",
            text,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(match, "SKILL.md must have one valid YAML frontmatter block")

        values: dict[str, str] = {}
        for line in match.group("frontmatter").splitlines():
            key_match = re.fullmatch(r"([a-z_]+):\s*(\S.*)", line)
            self.assertIsNotNone(key_match, f"Invalid frontmatter line: {line!r}")
            key, value = key_match.groups()
            self.assertNotIn(key, values, f"Duplicate frontmatter key: {key}")
            values[key] = value.strip().strip('"')
        return values, match.group("body")

    def test_skill_frontmatter_and_body_contract(self) -> None:
        frontmatter, body = self._skill_parts()

        self.assertEqual(set(frontmatter), {"name", "description"})
        self.assertEqual(frontmatter["name"], "dispatching-codex-workers")
        self.assertRegex(frontmatter["name"], r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
        self.assertTrue(frontmatter["description"].startswith("Use when"))
        self.assertLessEqual(len(frontmatter["description"]), 1024)
        self.assertLess(len(body.splitlines()), 500)
        self.assertRegex(
            body,
            r"(?im)^##\s+(?:Mandatory\s+)?Lifecycle Close-Loop(?: Checklist)?\s*$",
        )
        self.assertRegex(
            body,
            r"(?im)^(?=.*danger-full-access)(?=.*(?:never|do not|must not|prohibited)).+$",
        )

    def test_skill_reference_contains_deferred_runtime_details(self) -> None:
        skill = self._read_required(SKILL_PATH)
        design = self._read_required(DESIGN_PATH)

        self.assertIn("references/design.md", skill)
        for required_term in ("manifest", "Windows", "Linux", "macOS"):
            self.assertIn(required_term, design)

    def test_openai_metadata_uses_valid_quoted_strings(self) -> None:
        text = self._read_required(OPENAI_PATH)
        self.assertTrue(text.startswith("interface:\n"))
        matches = re.findall(r'^  ([a-z_]+): "([^"\r\n]+)"$', text, flags=re.MULTILINE)
        values = dict(matches)

        self.assertEqual(
            set(values),
            {"display_name", "short_description", "default_prompt"},
        )
        self.assertEqual(values["display_name"], "Dispatching Codex Workers")
        self.assertGreaterEqual(len(values["short_description"]), 25)
        self.assertLessEqual(len(values["short_description"]), 64)
        self.assertIn("$dispatching-codex-workers", values["default_prompt"])
        self.assertEqual(len(matches), 3)

    def test_public_skill_has_no_private_names_or_machine_paths(self) -> None:
        public_text = "\n".join(
            self._read_required(path)
            for path in (SKILL_PATH, DESIGN_PATH, OPENAI_PATH)
        )

        for forbidden in (
            "".join(("Lu", "na")),
            "".join(("Ter", "ra")),
            "".join(("gpt-5.6-", "lu", "na")),
            "".join(("E", ":/")),
        ):
            self.assertNotIn(forbidden, public_text)
        self.assertIsNone(
            re.search(r"(?i)\b[a-z]:\\", public_text),
            "Public skill must not contain backslash drive paths",
        )


if __name__ == "__main__":
    unittest.main()
