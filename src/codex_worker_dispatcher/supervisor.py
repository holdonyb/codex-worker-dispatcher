"""Own one detached runner and persist its terminal task state."""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

from .errors import WorkerError
from .process import (
    ProcessIdentity,
    owned_task_identity_matches,
    ownership_hash,
    read_process_identity,
    terminate_owned_tree,
    windows_detached_flags,
)
from .state import StateStore, TERMINAL_STATES, utc_now


_POLL_SECONDS = 0.2
_TERMINATION_GRACE_SECONDS = 1.0
_RUNNER_ARTIFACTS = ("events.jsonl", "stderr.log", "last-message.txt")


def _invalid_state(message: str, **details: object) -> WorkerError:
    return WorkerError("invalid_state", message, details)


def _timeout_seconds(manifest: dict[str, object]) -> float:
    value = manifest.get("timeout_sec")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid_state("Task timeout must be a number", field="timeout_sec")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise _invalid_state(
            "Task timeout must be finite and non-negative",
            field="timeout_sec",
        )
    return result


def _verify_nonce(manifest: dict[str, object], nonce: str) -> None:
    expected = manifest.get("ownership_nonce_hash")
    if not isinstance(expected, str) or not hmac.compare_digest(
        expected,
        ownership_hash(nonce),
    ):
        raise WorkerError(
            "process_identity_mismatch",
            "Ownership nonce does not match the task manifest",
            {"task_id": manifest.get("task_id")},
        )


def _current_owned_identity(nonce: str, task_dir: Path) -> ProcessIdentity:
    identity = read_process_identity(os.getpid())
    if not owned_task_identity_matches(
        identity,
        identity.start_marker,
        nonce,
        "supervisor",
        task_dir,
    ):
        raise WorkerError(
            "process_identity_mismatch",
            "Supervisor process identity does not match its task and role",
            {"pid": os.getpid()},
        )
    return identity


def _record_running_supervisor(
    store: StateStore,
    task_dir: Path,
    manifest: dict[str, object],
    identity: ProcessIdentity,
) -> dict[str, object]:
    def record(current: dict[str, object]) -> dict[str, object]:
        now = utc_now()
        current.update(
            {
                "status": "running",
                "updated_at": now,
                "started_at": current.get("started_at") or now,
                "supervisor_pid": identity.pid,
                "supervisor_start_marker": identity.start_marker,
            }
        )
        return current

    return store.update_manifest(task_dir, record)


def _runner_command(task_dir: Path, nonce: str) -> list[str]:
    executable = sys.executable
    if os.name == "nt":
        base_executable = getattr(sys, "_base_executable", None)
        if isinstance(base_executable, str) and base_executable:
            executable = base_executable
    return [
        executable,
        "-m",
        "codex_worker_dispatcher.runner",
        "--task-dir",
        str(task_dir),
        "--ownership-nonce",
        nonce,
    ]


def _launch_runner(task_dir: Path, nonce: str) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    existing_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        package_root
        if not existing_python_path
        else os.pathsep.join((package_root, existing_python_path))
    )
    keyword_arguments: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": environment,
    }
    if os.name == "nt":
        keyword_arguments["creationflags"] = windows_detached_flags()
    else:
        keyword_arguments["start_new_session"] = True
    try:
        return subprocess.Popen(
            _runner_command(task_dir, nonce),
            **keyword_arguments,
        )
    except OSError as error:
        raise WorkerError(
            "invalid_state",
            "Could not launch the worker runner",
            {"task_id": task_dir.name, "error": str(error)},
        ) from error


def _read_owned_runner_identity(
    runner: subprocess.Popen[bytes],
    nonce: str,
    task_dir: Path,
) -> ProcessIdentity:
    try:
        identity = read_process_identity(runner.pid)
    except WorkerError as error:
        return_code = runner.poll()
        if return_code is not None:
            raise WorkerError(
                "invalid_state",
                "Runner exited before its process identity could be recorded",
                {"pid": runner.pid, "exit_code": return_code},
            ) from error
        raise
    if not owned_task_identity_matches(
        identity,
        identity.start_marker,
        nonce,
        "runner",
        task_dir,
    ):
        raise WorkerError(
            "process_identity_mismatch",
            "Runner process identity does not match its task and role",
            {"pid": runner.pid},
        )
    return identity


def _record_runner(
    store: StateStore,
    task_dir: Path,
    manifest: dict[str, object],
    identity: ProcessIdentity,
) -> dict[str, object]:
    def record(current: dict[str, object]) -> dict[str, object]:
        current.update(
            {
                "updated_at": utc_now(),
                "runner_pid": identity.pid,
                "runner_start_marker": identity.start_marker,
                "runner_launching": False,
            }
        )
        return current

    return store.update_manifest(task_dir, record)


def _reserve_runner_launch(
    store: StateStore,
    task_dir: Path,
) -> dict[str, object]:
    def reserve(current: dict[str, object]) -> dict[str, object]:
        current.update(
            {
                "updated_at": utc_now(),
                "runner_launching": True,
            }
        )
        return current

    return store.update_manifest(task_dir, reserve)


def _runner_artifacts_ready(task_dir: Path) -> bool:
    for filename in _RUNNER_ARTIFACTS:
        path = task_dir / filename
        try:
            result = path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise _invalid_state(
                "Could not inspect runner output",
                filename=filename,
                error=str(error),
            ) from error
        if not stat.S_ISREG(result.st_mode) or stat.S_ISLNK(result.st_mode):
            raise _invalid_state(
                "Runner output is not a regular file",
                filename=filename,
            )
    return True


def _cancel_requested(task_dir: Path) -> bool:
    path = task_dir / "cancel.request"
    try:
        result = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise _invalid_state(
            "Could not inspect cancellation request",
            filename=path.name,
            error=str(error),
        ) from error
    if not stat.S_ISREG(result.st_mode) or stat.S_ISLNK(result.st_mode):
        raise _invalid_state(
            "Cancellation request is not a regular file",
            filename=path.name,
        )
    return True


def _stop_runner(
    runner: subprocess.Popen[bytes],
    identity: ProcessIdentity,
    nonce: str,
) -> int:
    return_code = runner.poll()
    if return_code is None:
        try:
            terminate_owned_tree(
                identity.pid,
                identity.start_marker,
                nonce,
                _TERMINATION_GRACE_SECONDS,
            )
        except WorkerError as error:
            if error.code not in {
                "process_not_found",
                "process_identity_mismatch",
            }:
                raise
            return_code = runner.poll()
            if return_code is None:
                try:
                    return_code = runner.wait(timeout=0)
                except subprocess.TimeoutExpired:
                    raise error
            if not isinstance(return_code, int):
                raise error
            return return_code
        try:
            return_code = runner.wait(timeout=2.0)
        except subprocess.TimeoutExpired as error:
            raise _invalid_state(
                "Reclaimed runner did not report its exit",
                pid=identity.pid,
            ) from error
    return return_code


def _best_effort_reclaim_runner(
    runner: subprocess.Popen[bytes],
    identity: ProcessIdentity | None,
    nonce: str,
    task_dir: Path,
) -> None:
    verified_identity = identity
    if verified_identity is None:
        try:
            candidate = read_process_identity(runner.pid)
        except (OSError, WorkerError):
            return
        if not owned_task_identity_matches(
            candidate,
            candidate.start_marker,
            nonce,
            "runner",
            task_dir,
        ):
            return
        verified_identity = candidate
    try:
        _stop_runner(runner, verified_identity, nonce)
    except (OSError, WorkerError, subprocess.SubprocessError):
        pass


def _terminal_manifest(
    manifest: dict[str, object],
    status: str,
    exit_code: int,
    error: str | None,
) -> dict[str, object]:
    if status not in TERMINAL_STATES:
        raise ValueError(f"Not a terminal task status: {status}")
    now = utc_now()
    updated = dict(manifest)
    updated.update(
        {
            "status": status,
            "updated_at": now,
            "completed_at": now,
            "exit_code": exit_code,
            "error": error,
            "runner_launching": False,
        }
    )
    return updated


def _persist_terminal(
    store: StateStore,
    task_dir: Path,
    manifest: dict[str, object],
    status: str,
    exit_code: int,
    error: str | None,
) -> dict[str, object]:
    def persist(current: dict[str, object]) -> dict[str, object]:
        return _terminal_manifest(current, status, exit_code, error)

    return store.update_manifest(task_dir, persist)


def supervise(task_dir: Path, nonce: str) -> str:
    """Supervise one runner to a terminal task state and return that state."""

    store = StateStore(task_dir.parent)
    manifest = store.read_manifest(task_dir)
    _verify_nonce(manifest, nonce)
    if manifest.get("status") in TERMINAL_STATES:
        return str(manifest["status"])
    timeout = _timeout_seconds(manifest)
    supervisor_identity = _current_owned_identity(nonce, task_dir)
    manifest = _record_running_supervisor(
        store,
        task_dir,
        manifest,
        supervisor_identity,
    )
    if manifest.get("status") in TERMINAL_STATES:
        return str(manifest["status"])

    runner: subprocess.Popen[bytes] | None = None
    runner_identity: ProcessIdentity | None = None
    deadline = time.monotonic() + timeout
    try:
        manifest = _reserve_runner_launch(store, task_dir)
        if manifest.get("status") in TERMINAL_STATES:
            return str(manifest["status"])
        runner = _launch_runner(task_dir, nonce)
        runner_identity = _read_owned_runner_identity(runner, nonce, task_dir)
        manifest = _record_runner(
            store,
            task_dir,
            manifest,
            runner_identity,
        )
        if manifest.get("status") in TERMINAL_STATES:
            _best_effort_reclaim_runner(
                runner,
                runner_identity,
                nonce,
                task_dir,
            )
            return str(manifest["status"])

        while True:
            return_code = runner.poll()
            if return_code is not None:
                if not _runner_artifacts_ready(task_dir):
                    terminal = _persist_terminal(
                        store,
                        task_dir,
                        manifest,
                        "failed",
                        return_code,
                        "Runner exited before creating observable outputs",
                    )
                    return str(terminal["status"])
                if return_code == 0:
                    terminal = _persist_terminal(
                        store,
                        task_dir,
                        manifest,
                        "completed",
                        return_code,
                        None,
                    )
                    return str(terminal["status"])
                terminal = _persist_terminal(
                    store,
                    task_dir,
                    manifest,
                    "failed",
                    return_code,
                    f"Runner exited with code {return_code}",
                )
                return str(terminal["status"])

            if _cancel_requested(task_dir):
                return_code = _stop_runner(runner, runner_identity, nonce)
                terminal = _persist_terminal(
                    store,
                    task_dir,
                    manifest,
                    "cancelled",
                    return_code,
                    "Task cancellation was requested",
                )
                return str(terminal["status"])

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return_code = _stop_runner(runner, runner_identity, nonce)
                terminal = _persist_terminal(
                    store,
                    task_dir,
                    manifest,
                    "timed_out",
                    return_code,
                    f"Worker TTL expired after {timeout:g} seconds",
                )
                return str(terminal["status"])
            _runner_artifacts_ready(task_dir)
            time.sleep(min(_POLL_SECONDS, remaining))
    except BaseException as error:
        if runner is not None:
            _best_effort_reclaim_runner(
                runner,
                runner_identity,
                nonce,
                task_dir,
            )
        try:
            _persist_terminal(
                store,
                task_dir,
                manifest,
                "failed",
                1,
                str(error) or error.__class__.__name__,
            )
        except (OSError, WorkerError, ValueError):
            pass
        raise


def _record_failure(
    task_dir: Path,
    nonce: str,
    error: BaseException,
) -> None:
    try:
        store = StateStore(task_dir.parent)
        manifest = store.read_manifest(task_dir)
        _verify_nonce(manifest, nonce)
        _current_owned_identity(nonce, task_dir)
        if manifest.get("status") not in TERMINAL_STATES:
            _persist_terminal(
                store,
                task_dir,
                manifest,
                "failed",
                1,
                str(error) or error.__class__.__name__,
            )
    except (OSError, WorkerError, ValueError):
        pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Supervise one Codex worker")
    parser.add_argument("--task-dir", required=True, type=Path)
    parser.add_argument("--ownership-nonce", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        supervise(arguments.task_dir, arguments.ownership_nonce)
        return 0
    except WorkerError as error:
        _record_failure(arguments.task_dir, arguments.ownership_nonce, error)
        print(json.dumps(error.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception as error:
        wrapped = WorkerError(
            "internal_error",
            "Unexpected supervisor failure",
            {"error": str(error)},
        )
        _record_failure(arguments.task_dir, arguments.ownership_nonce, wrapped)
        print(json.dumps(wrapped.to_dict(), ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main", "supervise"]
