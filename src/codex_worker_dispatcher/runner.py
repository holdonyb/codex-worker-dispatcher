"""Execute one fake or real Codex worker inside an owned runner process."""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
from typing import TextIO

from .errors import WorkerError
from .process import ownership_hash, windows_no_window_flags
from .state import StateStore, TERMINAL_STATES


_WINDOWS_BATCH_SUFFIXES = {".bat", ".cmd"}
_WINDOWS_NATIVE_SUFFIXES = {".com", ".exe"}
_CODEX_SCRIPT_PARTS = ("@openai", "codex", "bin", "codex.js")
# Windows identity inspection may consume three 10-second query attempts under
# load. Keep the runner alive through that budget plus process-start overhead.
_IDENTITY_RECORD_TIMEOUT_SECONDS = 45.0


def _invalid_manifest(message: str, **details: object) -> WorkerError:
    return WorkerError("invalid_state", message, details)


def _manifest_mapping(manifest: dict[str, object], key: str) -> dict[str, object]:
    value = manifest.get(key)
    if not isinstance(value, dict):
        raise _invalid_manifest(f"Manifest field must be an object: {key}", field=key)
    return value


def _manifest_string(
    manifest: dict[str, object],
    key: str,
    *,
    allow_empty: bool = False,
) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise _invalid_manifest(f"Manifest field must be a string: {key}", field=key)
    return value


def _manifest_number(manifest: dict[str, object], key: str) -> float:
    value = manifest.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid_manifest(f"Manifest field must be a number: {key}", field=key)
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise _invalid_manifest(
            f"Manifest field must be a finite non-negative number: {key}",
            field=key,
        )
    return number


def _manifest_exit_code(manifest: dict[str, object]) -> int:
    value = manifest.get("fake_exit_code", 0)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _invalid_manifest(
            "Manifest field must be an integer: fake_exit_code",
            field="fake_exit_code",
        )
    return value


def _create_output(path: Path) -> TextIO:
    try:
        return path.open("x", encoding="utf-8", newline="\n")
    except FileExistsError as error:
        raise _invalid_manifest(
            f"Worker output already exists: {path.name}",
            filename=path.name,
        ) from error
    except OSError as error:
        raise _invalid_manifest(
            f"Could not create worker output: {path.name}",
            filename=path.name,
            error=str(error),
        ) from error


def _read_prompt(task_dir: Path) -> str:
    return StateStore(task_dir.parent).read_prompt(task_dir)


def _run_fake(task_dir: Path, manifest: dict[str, object]) -> int:
    delay_seconds = _manifest_number(manifest, "fake_delay_sec")
    exit_code = _manifest_exit_code(manifest)
    prompt = _read_prompt(task_dir)

    with _create_output(task_dir / "events.jsonl") as events, _create_output(
        task_dir / "stderr.log"
    ), _create_output(task_dir / "last-message.txt") as last_message:
        deadline = time.monotonic() + delay_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))

        event_type = "fake.completed" if exit_code == 0 else "fake.failed"
        json.dump(
            {"type": event_type, "message": prompt},
            events,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        events.write("\n")
        events.flush()
        last_message.write(prompt)
        last_message.flush()
    return exit_code


def _optional_string(route: dict[str, object], key: str) -> str | None:
    value = route.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise _invalid_manifest(
            f"Route field must be a non-empty string or null: {key}",
            field=key,
        )
    return value


def _launcher_error(
    code: str,
    message: str,
    launcher: Path,
    **details: object,
) -> WorkerError:
    return WorkerError(
        code,
        message,
        {"launcher": str(launcher), **details},
    )


def _regular_launcher_path(path: Path, launcher: Path, kind: str) -> Path:
    try:
        result = path.lstat()
    except FileNotFoundError as error:
        raise _launcher_error(
            "codex_not_found",
            f"Codex launcher {kind} was not found",
            launcher,
            path=str(path),
        ) from error
    except OSError as error:
        raise _launcher_error(
            "invalid_state",
            f"Could not inspect Codex launcher {kind}",
            launcher,
            path=str(path),
            error=str(error),
        ) from error
    if stat.S_ISLNK(result.st_mode) or not stat.S_ISREG(result.st_mode):
        raise _launcher_error(
            "invalid_state",
            f"Codex launcher {kind} is not a regular file",
            launcher,
            path=str(path),
        )
    try:
        return path.resolve(strict=True)
    except FileNotFoundError as error:
        raise _launcher_error(
            "codex_not_found",
            f"Codex launcher {kind} disappeared during validation",
            launcher,
            path=str(path),
        ) from error
    except OSError as error:
        raise _launcher_error(
            "invalid_state",
            f"Could not resolve Codex launcher {kind}",
            launcher,
            path=str(path),
            error=str(error),
        ) from error


def _resolve_batch_launcher(launcher: Path) -> list[str]:
    resolved_launcher = _regular_launcher_path(launcher, launcher, "shim")
    if resolved_launcher.parent.name.casefold() == ".bin":
        script_entry = resolved_launcher.parent.parent.joinpath(*_CODEX_SCRIPT_PARTS)
    else:
        script_entry = resolved_launcher.parent.joinpath(
            "node_modules",
            *_CODEX_SCRIPT_PARTS,
        )
    script = _regular_launcher_path(script_entry, resolved_launcher, "script")

    node_value = shutil.which("node")
    if node_value is None:
        raise _launcher_error(
            "codex_not_found",
            "Codex launcher requires native Node.js but node was not found on PATH",
            resolved_launcher,
        )
    node_entry = Path(os.path.abspath(node_value))
    if node_entry.suffix.casefold() not in _WINDOWS_NATIVE_SUFFIXES:
        raise _launcher_error(
            "invalid_state",
            "Codex launcher resolved node to a non-native executable",
            resolved_launcher,
            node=str(node_entry),
        )
    node = _regular_launcher_path(node_entry, resolved_launcher, "node executable")
    return [str(node), str(script)]


def _resolve_codex_command() -> list[str]:
    launcher_value = shutil.which("codex")
    if launcher_value is None:
        raise WorkerError(
            "codex_not_found",
            "Codex CLI was not found on PATH",
            {},
        )
    launcher = Path(os.path.abspath(launcher_value))
    suffix = launcher.suffix.casefold()
    if suffix in _WINDOWS_BATCH_SUFFIXES:
        return _resolve_batch_launcher(launcher)
    if sys.platform == "win32" and suffix not in _WINDOWS_NATIVE_SUFFIXES:
        raise _launcher_error(
            "invalid_state",
            "Codex launcher is not a native Windows executable",
            launcher,
        )
    if sys.platform == "win32":
        native = _regular_launcher_path(launcher, launcher, "native executable")
        return [str(native)]
    return [launcher_value]


def _write_worker_error(stream: TextIO, error: WorkerError) -> None:
    json.dump(error.to_dict(), stream, ensure_ascii=False)
    stream.write("\n")
    stream.flush()


def _run_codex(task_dir: Path, manifest: dict[str, object]) -> int:
    workdir = Path(_manifest_string(manifest, "workdir"))
    if not workdir.is_dir():
        raise _invalid_manifest(
            "Worker work directory does not exist",
            workdir=str(workdir),
        )
    route = _manifest_mapping(manifest, "route")
    sandbox = _optional_string(route, "sandbox")
    if sandbox not in {"read-only", "workspace-write"}:
        raise _invalid_manifest(
            "Route sandbox is invalid",
            sandbox=sandbox,
        )
    model = _optional_string(route, "model")
    reasoning = _optional_string(route, "reasoning")
    skip_git_repo_check = manifest.get(
        "skip_git_repo_check",
        route.get("skip_git_repo_check", False),
    )
    if not isinstance(skip_git_repo_check, bool):
        raise _invalid_manifest(
            "Route skip_git_repo_check must be a boolean",
            field="skip_git_repo_check",
        )
    prompt = _read_prompt(task_dir)
    last_message_path = task_dir / "last-message.txt"
    with _create_output(task_dir / "events.jsonl") as events, _create_output(
        task_dir / "stderr.log"
    ) as stderr, _create_output(last_message_path):
        try:
            command_prefix = _resolve_codex_command()
        except WorkerError as error:
            _write_worker_error(stderr, error)
            raise error
        arguments = command_prefix + [
            "exec",
            "--ephemeral",
            "--json",
            "--color",
            "never",
            "--sandbox",
            sandbox,
            "-C",
            str(workdir),
            "-o",
            str(last_message_path),
        ]
        if model is not None:
            arguments.extend(["--model", model])
        if reasoning is not None:
            arguments.extend(["-c", f'model_reasoning_effort="{reasoning}"'])
        if skip_git_repo_check:
            arguments.append("--skip-git-repo-check")
        arguments.append("-")
        result = subprocess.run(
            arguments,
            input=prompt,
            stdout=events,
            stderr=stderr,
            cwd=workdir,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=windows_no_window_flags(),
        )
    return result.returncode


def run_task(task_dir: Path, manifest: dict[str, object]) -> int:
    """Run the engine selected by a validated task manifest."""

    engine = _manifest_string(manifest, "engine")
    if engine == "fake":
        return _run_fake(task_dir, manifest)
    if engine == "codex":
        return _run_codex(task_dir, manifest)
    raise _invalid_manifest("Worker engine is invalid", engine=engine)


def _load_owned_task(task_dir: Path, nonce: str) -> dict[str, object]:
    manifest = StateStore(task_dir.parent).read_manifest(task_dir)
    expected_hash = manifest.get("ownership_nonce_hash")
    actual_hash = ownership_hash(nonce)
    if not isinstance(expected_hash, str) or not hmac.compare_digest(
        expected_hash,
        actual_hash,
    ):
        raise WorkerError(
            "process_identity_mismatch",
            "Ownership nonce does not match the task manifest",
            {"task_id": manifest.get("task_id")},
        )
    return manifest


def _wait_for_identity_record(task_dir: Path, nonce: str) -> dict[str, object]:
    store = StateStore(task_dir.parent)
    deadline = time.monotonic() + _IDENTITY_RECORD_TIMEOUT_SECONDS
    while True:
        manifest = store.read_manifest(task_dir)
        expected_hash = manifest.get("ownership_nonce_hash")
        if not isinstance(expected_hash, str) or not hmac.compare_digest(
            expected_hash,
            ownership_hash(nonce),
        ):
            raise WorkerError(
                "process_identity_mismatch",
                "Ownership nonce changed before runner release",
                {"task_id": manifest.get("task_id")},
            )
        status = manifest.get("status")
        if status in TERMINAL_STATES:
            raise _invalid_manifest(
                "Task became terminal before runner release",
                pid=os.getpid(),
                status=status,
            )
        runner_start_marker = manifest.get("runner_start_marker")
        if (
            manifest.get("runner_pid") == os.getpid()
            and isinstance(runner_start_marker, str)
            and runner_start_marker
        ):
            return manifest
        if manifest.get("runner_launching") is not True:
            raise _invalid_manifest(
                "Runner launch reservation cleared before identity recording",
                pid=os.getpid(),
            )
        if time.monotonic() >= deadline:
            raise _invalid_manifest(
                "Supervisor did not record the runner identity before release",
                pid=os.getpid(),
            )
        time.sleep(0.02)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one Codex worker task")
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--ownership-nonce", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        _load_owned_task(arguments.task_dir, arguments.ownership_nonce)
        manifest = _wait_for_identity_record(
            arguments.task_dir,
            arguments.ownership_nonce,
        )
        return run_task(arguments.task_dir, manifest)
    except WorkerError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as error:
        print(
            json.dumps(
                WorkerError(
                    "internal_error",
                    "Unexpected runner failure",
                    {"error": str(error)},
                ).to_dict(),
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "run_task"]
