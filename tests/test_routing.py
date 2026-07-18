import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.routing import resolve_route


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workdir = Path(self.temporary_directory.name)

    def resolve(self, **overrides: object) -> dict[str, object]:
        arguments: dict[str, object] = {
            "prompt": "inspect the project",
            "workdir": self.workdir,
            "complexity": "simple",
            "intent": "read",
            "sandbox": "auto",
            "allowed_paths": [],
            "model": None,
            "reasoning": "auto",
        }
        arguments.update(overrides)
        return resolve_route(**arguments)  # type: ignore[arg-type]

    def test_simple_read_defaults_to_low_read_only_route(self) -> None:
        route = self.resolve()

        self.assertEqual(route["model"], None)
        self.assertEqual(route["reasoning"], "low")
        self.assertEqual(route["sandbox"], "read-only")
        self.assertEqual(route["intent"], "read")
        self.assertFalse(route["write_authorized"])
        self.assertEqual(route["allowed_paths"], [])

    def test_standard_write_authorizes_normalized_allowed_path(self) -> None:
        route = self.resolve(
            complexity="standard",
            intent="write",
            allowed_paths=["src/parser"],
            model="gpt-5.6",
        )

        self.assertEqual(route["model"], "gpt-5.6")
        self.assertEqual(route["reasoning"], "medium")
        self.assertEqual(route["sandbox"], "workspace-write")
        self.assertTrue(route["write_authorized"])
        self.assertEqual(
            route["allowed_paths"],
            [str((self.workdir / "src/parser").resolve(strict=False))],
        )

    def test_write_without_allowed_paths_remains_write_but_is_read_only(self) -> None:
        route = self.resolve(intent="write")

        self.assertEqual(route["intent"], "write")
        self.assertEqual(route["sandbox"], "read-only")
        self.assertFalse(route["write_authorized"])

    def test_workspace_write_requires_write_intent(self) -> None:
        with self.assertRaises(WorkerError) as raised:
            self.resolve(sandbox="workspace-write")

        self.assertEqual(raised.exception.code, "write_not_authorized")
        self.assertIn("requires write intent", raised.exception.message)

    def test_allowed_path_must_be_inside_work_directory(self) -> None:
        with self.assertRaises(WorkerError) as raised:
            self.resolve(intent="write", allowed_paths=["../outside"])

        self.assertEqual(raised.exception.code, "write_not_authorized")
        self.assertIn("inside the work directory", raised.exception.message)

    def test_allowed_path_cannot_be_empty_or_whitespace(self) -> None:
        for allowed_path in ("", "   ", "\t\n"):
            with self.subTest(allowed_path=allowed_path):
                with self.assertRaises(WorkerError) as raised:
                    self.resolve(intent="write", allowed_paths=[allowed_path])
                self.assertEqual(raised.exception.code, "invalid_arguments")
                self.assertIn("AllowedPath cannot be empty", raised.exception.message)

    def test_complexity_maps_to_reasoning(self) -> None:
        expected = {
            "simple": "low",
            "standard": "medium",
            "complex": "high",
            "hard": "xhigh",
            "extreme": "xhigh",
        }

        for complexity, reasoning in expected.items():
            with self.subTest(complexity=complexity):
                route = self.resolve(complexity=complexity)
                self.assertEqual(route["complexity"], complexity)
                self.assertEqual(route["reasoning"], reasoning)

    def test_auto_complexity_follows_resolved_intent(self) -> None:
        cases = (
            ({"intent": "read"}, "simple"),
            ({"intent": "write"}, "standard"),
            (
                {
                    "prompt": "implement the parser",
                    "intent": "auto",
                    "allowed_paths": ["src/parser"],
                },
                "standard",
            ),
            (
                {
                    "prompt": "implement the parser",
                    "intent": "auto",
                    "allowed_paths": [],
                },
                "simple",
            ),
        )

        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                route = self.resolve(complexity="auto", **overrides)
                self.assertEqual(route["complexity"], expected)

    def test_auto_intent_detects_write_verbs_only_with_allowed_paths(self) -> None:
        verbs = (
            "implement",
            "modify",
            "edit",
            "fix",
            "create",
            "update",
            "write",
            "patch",
            "refactor",
            "实现",
            "修改",
            "改造",
            "修复",
            "创建",
            "更新",
            "写入",
            "重构",
        )

        for verb in verbs:
            with self.subTest(verb=verb, allowed=True):
                route = self.resolve(
                    prompt=f"Please {verb} the parser",
                    intent="auto",
                    allowed_paths=["src/parser"],
                )
                self.assertEqual(route["intent"], "write")
            with self.subTest(verb=verb, allowed=False):
                route = self.resolve(prompt=f"Please {verb} the parser", intent="auto")
                self.assertEqual(route["intent"], "read")

    def test_auto_intent_detects_direct_write_imperative(self) -> None:
        route = self.resolve(
            prompt="modify the parser",
            intent="auto",
            allowed_paths=["src/parser"],
        )

        self.assertEqual(route["intent"], "write")

    def test_auto_intent_detects_add_remove_and_delete_imperatives(self) -> None:
        prompts = (
            "add parser tests",
            "remove the obsolete parser",
            "delete the generated fixture",
            "添加解析器测试",
            "新增解析器测试",
            "移除旧解析器",
            "删除生成的夹具",
        )

        for prompt in prompts:
            with self.subTest(prompt=prompt):
                route = self.resolve(
                    prompt=prompt,
                    intent="auto",
                    allowed_paths=["src/parser"],
                )
                self.assertEqual(route["intent"], "write")

    def test_auto_intent_rejects_negated_write_signals(self) -> None:
        for prompt in (
            "do not modify or edit anything",
            "never delete files",
        ):
            with self.subTest(prompt=prompt):
                route = self.resolve(
                    prompt=prompt,
                    intent="auto",
                    allowed_paths=["src/parser"],
                )
                self.assertEqual(route["intent"], "read")

    def test_auto_intent_rejects_explanatory_and_hypothetical_prompts(self) -> None:
        for prompt in (
            "explain how to modify the parser",
            "describe how to edit this file",
            "how would you refactor this module?",
        ):
            with self.subTest(prompt=prompt):
                route = self.resolve(
                    prompt=prompt,
                    intent="auto",
                    allowed_paths=["src/parser"],
                )
                self.assertEqual(route["intent"], "read")

    def test_auto_intent_defaults_ambiguous_prompts_to_read_only(self) -> None:
        for prompt in (
            "how to update the parser?",
            "can you explain how to modify the parser?",
            "please don't modify the parser",
            "don’t modify the parser",
            "解释如何修改解析器",
            "别修改，只检查",
            "请不要删除文件",
        ):
            with self.subTest(prompt=prompt):
                route = self.resolve(
                    prompt=prompt,
                    intent="auto",
                    allowed_paths=["src/parser"],
                )
                self.assertEqual(route["intent"], "read")
                self.assertEqual(route["sandbox"], "read-only")

    def test_auto_intent_preserves_later_scope_constraints(self) -> None:
        for prompt in (
            "fix the parser without changing tests",
            "update the parser; do not modify fixtures",
            "修改解析器，不要改测试",
        ):
            with self.subTest(prompt=prompt):
                route = self.resolve(
                    prompt=prompt,
                    intent="auto",
                    allowed_paths=["src/parser"],
                )
                self.assertEqual(route["intent"], "write")
                self.assertEqual(route["sandbox"], "workspace-write")

    def test_explicit_write_intent_ignores_inference_guards(self) -> None:
        for prompt in (
            "do not modify or edit anything",
            "explain how to modify the parser",
        ):
            with self.subTest(prompt=prompt):
                route = self.resolve(
                    prompt=prompt,
                    intent="write",
                    allowed_paths=["src/parser"],
                )
                self.assertEqual(route["intent"], "write")

    def test_explicit_reasoning_overrides_complexity_mapping(self) -> None:
        for reasoning in ("low", "medium", "high", "xhigh"):
            with self.subTest(reasoning=reasoning):
                route = self.resolve(complexity="extreme", reasoning=reasoning)
                self.assertEqual(route["reasoning"], reasoning)

    def test_invalid_route_options_raise_invalid_arguments(self) -> None:
        cases = (
            ("complexity", "unknown"),
            ("intent", "unknown"),
            ("sandbox", "unknown"),
            ("sandbox", "danger-full-access"),
            ("reasoning", "unknown"),
        )

        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaises(WorkerError) as raised:
                    self.resolve(**{field: value})
                self.assertEqual(raised.exception.code, "invalid_arguments")

    def test_commonpath_value_error_is_not_authorized(self) -> None:
        with patch(
            "codex_worker_dispatcher.routing.os.path.commonpath",
            side_effect=ValueError("Paths don't have the same drive"),
        ):
            with self.assertRaises(WorkerError) as raised:
                self.resolve(intent="write", allowed_paths=["src/parser"])

        self.assertEqual(raised.exception.code, "write_not_authorized")
        self.assertIn("inside the work directory", raised.exception.message)

    @unittest.skipUnless(os.name == "nt", "Windows-only cross-drive check")
    def test_real_cross_drive_allowed_path_is_not_authorized(self) -> None:
        workdir_drive = self.workdir.resolve(strict=False).drive.casefold()
        other_drive = next(
            (
                root
                for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                if (root := Path(f"{letter}:\\")).drive.casefold() != workdir_drive
                and root.exists()
            ),
            None,
        )
        if other_drive is None:
            self.skipTest("No alternate drive root is mounted")

        with self.assertRaises(WorkerError) as raised:
            self.resolve(intent="write", allowed_paths=[str(other_drive)])

        self.assertEqual(raised.exception.code, "write_not_authorized")
        self.assertIn("inside the work directory", raised.exception.message)

    def test_workdir_and_nested_nonexistent_allowed_paths_are_valid(self) -> None:
        nested = self.workdir / "missing" / "nested" / ".." / "target"
        route = self.resolve(
            intent="write",
            allowed_paths=[str(self.workdir), str(nested)],
        )

        self.assertEqual(
            route["allowed_paths"],
            [
                str(self.workdir.resolve(strict=False)),
                str(nested.resolve(strict=False)),
            ],
        )
        self.assertTrue(route["write_authorized"])


if __name__ == "__main__":
    unittest.main()
