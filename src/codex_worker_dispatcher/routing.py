import os
import re
from pathlib import Path

from codex_worker_dispatcher.errors import WorkerError


REASONING_BY_COMPLEXITY = {
    "simple": "low",
    "standard": "medium",
    "complex": "high",
    "hard": "xhigh",
    "extreme": "xhigh",
}

_VALID_OPTIONS = {
    "complexity": {"auto", *REASONING_BY_COMPLEXITY},
    "intent": {"auto", "read", "write"},
    "sandbox": {"auto", "read-only", "workspace-write"},
    "reasoning": {"auto", "low", "medium", "high", "xhigh"},
}
_ENGLISH_DIRECT_WRITE = re.compile(
    r"^(?:please\s+)?"
    r"(?:implement|modify|edit|fix|create|update|write|patch|refactor"
    r"|add|remove|delete)\b",
    re.IGNORECASE,
)
_CHINESE_DIRECT_WRITE = re.compile(
    r"^(?:(?:请帮我|帮我|麻烦|请)\s*|please\s+)?"
    r"(?:实现|修改|改造|修复|创建|更新|写入|重构|添加|新增|移除|删除)",
    re.IGNORECASE,
)
_APOSTROPHE_TRANSLATION = str.maketrans({"‘": "'", "’": "'"})


def _has_write_signal(prompt: str) -> bool:
    normalized_prompt = prompt.translate(_APOSTROPHE_TRANSLATION).strip()
    return bool(
        _ENGLISH_DIRECT_WRITE.search(normalized_prompt)
        or _CHINESE_DIRECT_WRITE.search(normalized_prompt)
    )


def _validate_option(field: str, value: str) -> None:
    if value not in _VALID_OPTIONS[field]:
        raise WorkerError(
            "invalid_arguments",
            f"Invalid {field}: {value}",
            {"field": field, "value": value},
        )


def _normalize_allowed_paths(workdir: Path, allowed_paths: list[str]) -> list[str]:
    resolved_workdir = workdir.resolve(strict=False)
    comparable_workdir = os.path.normcase(str(resolved_workdir))
    normalized_paths: list[str] = []

    for allowed_path in allowed_paths:
        if not allowed_path.strip():
            raise WorkerError(
                "invalid_arguments",
                "AllowedPath cannot be empty",
                {"path": allowed_path},
            )
        path = Path(allowed_path)
        if not path.is_absolute():
            path = resolved_workdir / path
        resolved_path = path.resolve(strict=False)
        comparable_path = os.path.normcase(str(resolved_path))
        try:
            common_path = os.path.normcase(
                os.path.commonpath([comparable_workdir, comparable_path])
            )
        except ValueError as error:
            raise WorkerError(
                "write_not_authorized",
                "Allowed paths must be inside the work directory",
                {"path": allowed_path},
            ) from error
        if common_path != comparable_workdir:
            raise WorkerError(
                "write_not_authorized",
                "Allowed paths must be inside the work directory",
                {"path": allowed_path},
            )
        normalized_paths.append(str(resolved_path))

    return normalized_paths


def resolve_route(
    prompt: str,
    workdir: Path,
    complexity: str,
    intent: str,
    sandbox: str,
    allowed_paths: list[str],
    model: str | None,
    reasoning: str,
) -> dict[str, object]:
    for field, value in (
        ("complexity", complexity),
        ("intent", intent),
        ("sandbox", sandbox),
        ("reasoning", reasoning),
    ):
        _validate_option(field, value)

    normalized_allowed_paths = _normalize_allowed_paths(workdir, allowed_paths)
    if intent == "auto":
        resolved_intent = (
            "write"
            if normalized_allowed_paths and _has_write_signal(prompt)
            else "read"
        )
    else:
        resolved_intent = intent

    resolved_complexity = complexity
    if complexity == "auto":
        resolved_complexity = "standard" if resolved_intent == "write" else "simple"

    resolved_reasoning = reasoning
    if reasoning == "auto":
        resolved_reasoning = REASONING_BY_COMPLEXITY[resolved_complexity]

    write_authorized = (
        resolved_intent == "write" and len(normalized_allowed_paths) > 0
    )
    if sandbox == "workspace-write" and not write_authorized:
        raise WorkerError(
            "write_not_authorized",
            "workspace-write requires write intent and at least one allowed path",
            {},
        )
    resolved_sandbox = sandbox
    if sandbox == "auto":
        resolved_sandbox = "workspace-write" if write_authorized else "read-only"

    return {
        "model": model,
        "reasoning": resolved_reasoning,
        "complexity": resolved_complexity,
        "intent": resolved_intent,
        "sandbox": resolved_sandbox,
        "write_authorized": write_authorized,
        "allowed_paths": normalized_allowed_paths,
    }
