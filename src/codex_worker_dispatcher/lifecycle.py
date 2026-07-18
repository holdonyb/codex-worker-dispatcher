"""Expose safe controller actions for one persisted worker lifecycle."""

from __future__ import annotations

from datetime import datetime, timezone
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import threading
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
from .routing import resolve_route
from .state import (
    StateStore,
    TERMINAL_STATES,
    default_state_root,
    utc_now,
    valid_task_id,
)


_POLL_SECONDS = 0.1
_REAP_GRACE_SECONDS = 1.0
_REAP_RESERVATION_HANDSHAKE_SECONDS = 30.0
_INACTIVE_RECONCILE_SECONDS = 0.5
_START_OBSERVE_SECONDS = 10.0
_START_CLEANUP_HANDSHAKE_SECONDS = 10.0
_RUNNER_RESERVATION_HANDSHAKE_SECONDS = 30.0
_NONCE_CANDIDATE = re.compile(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])")
_COMPLETION_EVENTS = {"turn.completed", "fake.completed"}


def _invalid_arguments(message: str, **details: object) -> WorkerError:
    return WorkerError("invalid_arguments", message, details)


def _invalid_state(message: str, **details: object) -> WorkerError:
    return WorkerError("invalid_state", message, details)


def _finite_nonnegative(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid_arguments(f"{field} must be a number", field=field)
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise _invalid_arguments(
            f"{field} must be finite and non-negative",
            field=field,
        )
    return number


def _resolved_workdir(workdir: Path) -> Path:
    path = Path(workdir).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise _invalid_arguments(
            "Work directory does not exist",
            workdir=str(path),
        ) from error
    if not resolved.is_dir():
        raise _invalid_arguments(
            "Work directory is not a directory",
            workdir=str(path),
        )
    return resolved


def _store(state_root: Path | None) -> StateStore:
    return StateStore(default_state_root() if state_root is None else Path(state_root))


def route_task(
    prompt: str,
    workdir: Path,
    complexity: str = "auto",
    intent: str = "auto",
    sandbox: str = "auto",
    allowed_paths: list[str] | None = None,
    model: str | None = None,
    reasoning: str = "auto",
) -> dict[str, object]:
    """Resolve a worker route without creating state or starting a process."""

    if not isinstance(prompt, str) or not prompt.strip():
        raise _invalid_arguments("Prompt cannot be empty", field="prompt")
    resolved_workdir = _resolved_workdir(workdir)
    return resolve_route(
        prompt,
        resolved_workdir,
        complexity,
        intent,
        sandbox,
        list(allowed_paths or []),
        model,
        reasoning,
    )


def _new_task_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{timestamp}-{secrets.token_hex(4)}"


def _supervisor_command(task_dir: Path, nonce: str) -> list[str]:
    executable = sys.executable
    if os.name == "nt":
        base_executable = getattr(sys, "_base_executable", None)
        if isinstance(base_executable, str) and base_executable:
            executable = base_executable
    return [
        executable,
        "-m",
        "codex_worker_dispatcher.supervisor",
        "--task-dir",
        str(task_dir),
        "--ownership-nonce",
        nonce,
    ]


def _launch_supervisor(task_dir: Path, nonce: str) -> subprocess.Popen[bytes]:
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
            _supervisor_command(task_dir, nonce),
            **keyword_arguments,
        )
    except OSError as error:
        raise _invalid_state(
            "Could not launch the worker supervisor",
            task_id=task_dir.name,
            error=str(error),
        ) from error


def _launched_identity(
    process: subprocess.Popen[bytes],
    nonce: str,
    task_dir: Path,
) -> ProcessIdentity:
    deadline = time.monotonic() + 5.0
    while True:
        try:
            identity = read_process_identity(process.pid)
        except WorkerError as error:
            if process.poll() is not None or time.monotonic() >= deadline:
                raise _invalid_state(
                    "Supervisor exited before its identity was recorded",
                    pid=process.pid,
                    exit_code=process.poll(),
                ) from error
            time.sleep(0.01)
            continue
        if not owned_task_identity_matches(
            identity,
            identity.start_marker,
            nonce,
            "supervisor",
            task_dir,
        ):
            if process.poll() is not None or time.monotonic() >= deadline:
                raise WorkerError(
                    "process_identity_mismatch",
                    "Launched supervisor identity does not match its task and role",
                    {"pid": process.pid},
                )
            time.sleep(0.01)
            continue
        return identity


def _wait_for_supervisor_manifest(
    store: StateStore,
    task_dir: Path,
    process: subprocess.Popen[bytes],
    identity: ProcessIdentity,
) -> dict[str, object]:
    deadline = time.monotonic() + _START_OBSERVE_SECONDS
    while True:
        current = store.read_manifest(task_dir)
        if current.get("status") in TERMINAL_STATES:
            return current
        recorded_pid = current.get("supervisor_pid")
        recorded_start = current.get("supervisor_start_marker")
        if (
            recorded_pid == identity.pid
            and recorded_start == identity.start_marker
        ):
            return current
        if recorded_pid is not None or recorded_start is not None:
            raise WorkerError(
                "process_identity_mismatch",
                "Supervisor persisted an unexpected process identity",
                {
                    "task_id": task_dir.name,
                    "pid": recorded_pid,
                },
            )
        return_code = process.poll()
        if return_code is not None:
            raise _invalid_state(
                "Supervisor exited before persisting its process identity",
                task_id=task_dir.name,
                pid=identity.pid,
                exit_code=return_code,
            )
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _invalid_state(
                "Supervisor did not persist its process identity in time",
                task_id=task_dir.name,
                pid=identity.pid,
            )
        time.sleep(min(0.02, remaining))


def _reap_popen_handle(process: subprocess.Popen[bytes]) -> None:
    """Reap the direct child without tying the detached worker to the caller."""

    def wait_for_exit() -> None:
        try:
            process.wait()
        except (OSError, subprocess.SubprocessError):
            pass

    threading.Thread(
        target=wait_for_exit,
        name=f"codex-worker-supervisor-{process.pid}",
        daemon=True,
    ).start()


def _failed_start_manifest(
    store: StateStore,
    task_dir: Path,
    error: BaseException,
) -> None:
    try:
        def mark_failed(current: dict[str, object]) -> dict[str, object]:
            now = utc_now()
            current.update(
                {
                    "status": "failed",
                    "updated_at": now,
                    "completed_at": now,
                    "exit_code": 1,
                    "error": str(error) or error.__class__.__name__,
                    "runner_launching": False,
                }
            )
            return current

        store.update_manifest(task_dir, mark_failed)
    except (OSError, WorkerError):
        pass


def _cleanup_store(
    store: StateStore,
    task_dir: Path,
) -> tuple[StateStore, ...]:
    fallback = StateStore(task_dir.parent)
    if isinstance(store, StateStore) and store.root == fallback.root:
        return (store,)
    return store, fallback


def _latest_cleanup_manifest(
    stores: tuple[StateStore, ...],
    task_dir: Path,
) -> dict[str, object] | None:
    for candidate in stores:
        try:
            return candidate.read_manifest(task_dir)
        except (OSError, RuntimeError, WorkerError):
            continue
    return None


def _exact_popen_cleanup(
    supervisor: subprocess.Popen[bytes],
    launched_identity: ProcessIdentity | None,
    nonce: str,
    task_dir: Path,
) -> bool:
    if supervisor.poll() is not None:
        try:
            supervisor.wait(timeout=0)
        except (OSError, subprocess.SubprocessError):
            pass
        return True
    try:
        current = read_process_identity(supervisor.pid)
    except WorkerError as error:
        if error.code != "process_not_found":
            return False
        try:
            supervisor.wait(timeout=2.0)
            return True
        except (OSError, subprocess.SubprocessError):
            return supervisor.poll() is not None
    expected_start = (
        current.start_marker
        if launched_identity is None
        else launched_identity.start_marker
    )
    if not owned_task_identity_matches(
        current,
        expected_start,
        nonce,
        "supervisor",
        task_dir,
    ):
        return False
    try:
        supervisor.terminate()
        supervisor.wait(timeout=1.0)
        return True
    except subprocess.TimeoutExpired:
        try:
            supervisor.kill()
            supervisor.wait(timeout=2.0)
            return True
        except (OSError, subprocess.SubprocessError):
            return supervisor.poll() is not None
    except (OSError, subprocess.SubprocessError):
        return supervisor.poll() is not None


def _cleanup_failed_start(
    store: StateStore,
    task_dir: Path,
    supervisor: subprocess.Popen[bytes] | None,
    launched_identity: ProcessIdentity | None,
    nonce: str,
) -> bool:
    if supervisor is None:
        return True
    try:
        _write_cancel_request(task_dir)
    except (OSError, WorkerError):
        pass

    stores = _cleanup_store(store, task_dir)
    latest: dict[str, object] | None = None
    deadline = time.monotonic() + _START_CLEANUP_HANDSHAKE_SECONDS
    reservation_deadline: float | None = None
    while True:
        candidate = _latest_cleanup_manifest(stores, task_dir)
        if candidate is not None:
            latest = candidate
            runner_recorded = (
                candidate.get("runner_pid") is not None
                or candidate.get("runner_start_marker") is not None
            )
            if candidate.get("status") in TERMINAL_STATES or runner_recorded:
                break
        if supervisor.poll() is not None:
            break
        now = time.monotonic()
        runner_launch_reserved = bool(
            latest is not None
            and latest.get("runner_launching") is True
            and latest.get("runner_pid") is None
            and latest.get("runner_start_marker") is None
        )
        if runner_launch_reserved:
            if reservation_deadline is None:
                reservation_deadline = (
                    now + _RUNNER_RESERVATION_HANDSHAKE_SECONDS
                )
            remaining = reservation_deadline - now
            if remaining <= 0:
                # The supervisor is still the only process that can identify and
                # reclaim a runner in this launch window. Never terminate it.
                return False
        else:
            remaining = deadline - now
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))

    latest = _latest_cleanup_manifest(stores, task_dir) or latest
    if (
        latest is not None
        and latest.get("runner_launching") is True
        and latest.get("runner_pid") is None
        and latest.get("runner_start_marker") is None
        and supervisor.poll() is None
    ):
        return False
    if latest is not None:
        try:
            owned = {
                role: _owned_process(latest, role, task_dir)
                for role in ("runner", "supervisor")
            }
        except WorkerError:
            return False
        for role in ("runner", "supervisor"):
            process = owned[role]
            if process is None:
                continue
            identity, owned_nonce = process
            try:
                terminate_owned_tree(
                    identity.pid,
                    identity.start_marker,
                    owned_nonce,
                    _REAP_GRACE_SECONDS,
                )
            except WorkerError as error:
                if error.code != "process_not_found":
                    return False

    return _exact_popen_cleanup(
        supervisor,
        launched_identity,
        nonce,
        task_dir,
    )


def start_task(
    *,
    prompt: str,
    workdir: Path,
    state_root: Path | None = None,
    complexity: str = "auto",
    intent: str = "auto",
    sandbox: str = "auto",
    allowed_paths: list[str] | None = None,
    model: str | None = None,
    reasoning: str = "auto",
    timeout_sec: float = 600.0,
    task_id: str | None = None,
    skip_git_repo_check: bool = False,
    engine: str = "codex",
    fake_delay_sec: float = 0.0,
    fake_exit_code: int = 0,
) -> dict[str, object]:
    """Persist a schema-v2 task and launch its detached supervisor."""

    if not isinstance(prompt, str) or not prompt.strip():
        raise _invalid_arguments("Prompt cannot be empty", field="prompt")
    resolved_workdir = _resolved_workdir(workdir)
    timeout = _finite_nonnegative(timeout_sec, "timeout_sec")
    fake_delay = _finite_nonnegative(fake_delay_sec, "fake_delay_sec")
    if engine not in {"codex", "fake"}:
        raise _invalid_arguments("Engine must be codex or fake", engine=engine)
    if isinstance(fake_exit_code, bool) or not isinstance(fake_exit_code, int):
        raise _invalid_arguments(
            "fake_exit_code must be an integer",
            field="fake_exit_code",
        )
    if not isinstance(skip_git_repo_check, bool):
        raise _invalid_arguments(
            "skip_git_repo_check must be a boolean",
            field="skip_git_repo_check",
        )
    selected_task_id = _new_task_id() if task_id is None else task_id
    if not isinstance(selected_task_id, str) or not valid_task_id(selected_task_id):
        raise WorkerError(
            "invalid_task_id",
            f"Invalid task ID: {selected_task_id}",
            {"task_id": selected_task_id},
        )
    route = route_task(
        prompt,
        resolved_workdir,
        complexity,
        intent,
        sandbox,
        allowed_paths,
        model,
        reasoning,
    )
    store = _store(state_root)
    task_dir = store.create_task_dir(selected_task_id)
    nonce = secrets.token_hex(16)
    now = utc_now()
    manifest: dict[str, object] = {
        "schema_version": 2,
        "task_id": selected_task_id,
        "status": "queued",
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "completed_at": None,
        "workdir": str(resolved_workdir),
        "route": route,
        "allowed_paths": list(route.get("allowed_paths", [])),
        "timeout_sec": timeout,
        "engine": engine,
        "fake_delay_sec": fake_delay,
        "fake_exit_code": fake_exit_code,
        "skip_git_repo_check": skip_git_repo_check,
        "ownership_nonce_hash": ownership_hash(nonce),
        "supervisor_pid": None,
        "supervisor_start_marker": None,
        "runner_pid": None,
        "runner_start_marker": None,
        "runner_launching": False,
        "exit_code": None,
        "error": None,
    }
    supervisor: subprocess.Popen[bytes] | None = None
    launched_identity: ProcessIdentity | None = None
    try:
        store.write_prompt(task_dir, prompt)
        store.write_manifest(task_dir, manifest)
        supervisor = _launch_supervisor(task_dir, nonce)
        launched_identity = _launched_identity(supervisor, nonce, task_dir)
        current = _wait_for_supervisor_manifest(
            store,
            task_dir,
            supervisor,
            launched_identity,
        )
        _reap_popen_handle(supervisor)
        return current
    except BaseException as error:
        safe_to_write = _cleanup_failed_start(
            store,
            task_dir,
            supervisor,
            launched_identity,
            nonce,
        )
        if (
            safe_to_write
            and not (
                isinstance(error, WorkerError)
                and error.code == "process_identity_mismatch"
            )
        ):
            _failed_start_manifest(StateStore(task_dir.parent), task_dir, error)
        raise


def _manifest_for_task(
    task_id: str,
    state_root: Path | None,
) -> tuple[StateStore, Path, dict[str, object]]:
    store = _store(state_root)
    task_dir = store.task_dir(task_id)
    return store, task_dir, store.read_manifest(task_dir)


def _manifest_nonce_hash(manifest: dict[str, object]) -> str:
    value = manifest.get("ownership_nonce_hash")
    if not isinstance(value, str) or len(value) != 64:
        raise _invalid_state(
            "Task ownership nonce hash is invalid",
            task_id=manifest.get("task_id"),
        )
    return value


def _nonce_from_identity(
    identity: ProcessIdentity,
    expected_hash: str,
) -> str | None:
    for candidate in _NONCE_CANDIDATE.findall(identity.command_line):
        if hmac.compare_digest(ownership_hash(candidate), expected_hash):
            return candidate
    return None


def _owned_process(
    manifest: dict[str, object],
    role: str,
    task_dir: Path,
) -> tuple[ProcessIdentity, str] | None:
    pid = manifest.get(f"{role}_pid")
    start_marker = manifest.get(f"{role}_start_marker")
    if pid is None and start_marker is None:
        return None
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(start_marker, str)
        or not start_marker
    ):
        raise _invalid_state(
            f"Recorded {role} identity is invalid",
            task_id=manifest.get("task_id"),
            role=role,
        )
    try:
        identity = read_process_identity(pid)
    except WorkerError as error:
        if error.code == "process_not_found":
            return None
        raise
    expected_hash = _manifest_nonce_hash(manifest)
    nonce = _nonce_from_identity(identity, expected_hash)
    if (
        identity.pid != pid
        or identity.start_marker != start_marker
        or nonce is None
        or not owned_task_identity_matches(
            identity,
            start_marker,
            nonce,
            role,
            task_dir,
        )
    ):
        raise WorkerError(
            "process_identity_mismatch",
            f"Recorded {role} identity does not match the owned worker",
            {"task_id": manifest.get("task_id"), "role": role, "pid": pid},
        )
    return identity, nonce


def _task_has_live_process(
    manifest: dict[str, object],
    task_dir: Path,
) -> bool:
    processes = [
        _owned_process(manifest, role, task_dir)
        for role in ("runner", "supervisor")
    ]
    return any(process is not None for process in processes)


def _read_regular_text(task_dir: Path, filename: str) -> str | None:
    path = task_dir / filename
    try:
        result = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _invalid_state(
            "Could not inspect task output",
            task_id=task_dir.name,
            filename=filename,
        ) from error
    if not stat.S_ISREG(result.st_mode) or stat.S_ISLNK(result.st_mode):
        raise _invalid_state(
            "Task output is not a regular file",
            task_id=task_dir.name,
            filename=filename,
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise _invalid_state(
            "Could not read task output",
            task_id=task_dir.name,
            filename=filename,
        ) from error


def _has_completion_event(task_dir: Path) -> bool:
    events = _read_regular_text(task_dir, "events.jsonl")
    if events is None:
        return False
    for line in events.splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") in _COMPLETION_EVENTS:
            return True
    return False


def _has_completion_evidence(task_dir: Path) -> bool:
    last_message = _read_regular_text(task_dir, "last-message.txt")
    return bool(last_message and last_message.strip()) and _has_completion_event(
        task_dir
    )


def _reconcile(
    store: StateStore,
    task_dir: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    if manifest.get("status") in TERMINAL_STATES:
        return manifest
    try:
        if _task_has_live_process(manifest, task_dir):
            return manifest
    except WorkerError as error:
        if (
            error.code != "process_identity_mismatch"
            or not _has_completion_evidence(task_dir)
        ):
            raise
    # A process can disappear immediately before its supervisor's final atomic
    # manifest write. Re-read both the manifest and both verified process
    # identities for a bounded interval before orphan reconciliation wins.
    deadline = time.monotonic() + _INACTIVE_RECONCILE_SECONDS
    while True:
        refreshed = store.read_manifest(task_dir)
        if refreshed.get("status") in TERMINAL_STATES:
            return refreshed
        try:
            if _task_has_live_process(refreshed, task_dir):
                return refreshed
        except WorkerError as error:
            if (
                error.code != "process_identity_mismatch"
                or not _has_completion_evidence(task_dir)
            ):
                raise
        manifest = refreshed
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(0.05, remaining))
    completed = _has_completion_evidence(task_dir)
    now = utc_now()
    updated = dict(manifest)
    updated.update(
        {
            "status": "completed" if completed else "orphaned",
            "updated_at": now,
            "completed_at": now,
            "exit_code": 0 if completed and updated.get("exit_code") is None else updated.get("exit_code"),
            "error": None
            if completed
            else "Task became inactive without complete completion evidence",
            "runner_launching": False,
        }
    )
    def reconcile_if_unchanged(
        current: dict[str, object],
    ) -> dict[str, object]:
        if current != manifest:
            return current
        return updated

    return store.update_manifest(task_dir, reconcile_if_unchanged)


def status_task(
    task_id: str,
    state_root: Path | None = None,
) -> dict[str, object]:
    store, task_dir, manifest = _manifest_for_task(task_id, state_root)
    return _reconcile(store, task_dir, manifest)


def wait_task(
    task_id: str,
    state_root: Path | None = None,
    wait_timeout_sec: float = 600.0,
) -> dict[str, object]:
    timeout = _finite_nonnegative(wait_timeout_sec, "wait_timeout_sec")
    deadline = time.monotonic() + timeout
    while True:
        manifest = status_task(task_id, state_root)
        if manifest.get("status") in TERMINAL_STATES:
            return manifest
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise WorkerError(
                "wait_timeout",
                "Controller wait deadline expired",
                {"task_id": task_id, "wait_timeout_sec": timeout},
            )
        time.sleep(min(_POLL_SECONDS, remaining))


def result_task(
    task_id: str,
    state_root: Path | None = None,
) -> dict[str, object]:
    manifest = status_task(task_id, state_root)
    if manifest.get("status") not in TERMINAL_STATES:
        raise WorkerError(
            "task_not_terminal",
            "Task has not reached a terminal state",
            {"task_id": task_id, "status": manifest.get("status")},
        )
    task_dir = _store(state_root).task_dir(task_id)
    result = dict(manifest)
    result["last_message"] = _read_regular_text(task_dir, "last-message.txt")
    return result


def _write_cancel_request(task_dir: Path) -> None:
    path = task_dir / "cancel.request"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        try:
            current = path.lstat()
        except OSError as error:
            raise _invalid_state(
                "Could not inspect cancellation request",
                task_id=task_dir.name,
            ) from error
        if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
            raise _invalid_state(
                "Cancellation request is not a regular file",
                task_id=task_dir.name,
            )
        return
    except OSError as error:
        raise _invalid_state(
            "Could not create cancellation request",
            task_id=task_dir.name,
        ) from error
    try:
        os.write(descriptor, b"cancel\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cancel_task(
    task_id: str,
    state_root: Path | None = None,
    wait_timeout_sec: float = 30.0,
) -> dict[str, object]:
    manifest = status_task(task_id, state_root)
    if manifest.get("status") in TERMINAL_STATES:
        return manifest
    task_dir = _store(state_root).task_dir(task_id)
    _write_cancel_request(task_dir)
    return wait_task(task_id, state_root, wait_timeout_sec)


def _runner_launch_identity_pending(manifest: dict[str, object]) -> bool:
    return bool(
        manifest.get("status") not in TERMINAL_STATES
        and manifest.get("runner_launching") is True
        and manifest.get("runner_pid") is None
        and manifest.get("runner_start_marker") is None
    )


def _wait_for_reapable_manifest(
    store: StateStore,
    task_dir: Path,
    manifest: dict[str, object],
) -> dict[str, object]:
    if not _runner_launch_identity_pending(manifest):
        return manifest
    if (
        manifest.get("supervisor_pid") is None
        and manifest.get("supervisor_start_marker") is None
    ):
        # A reservation without any supervisor identity cannot represent the
        # post-Popen window. Treat it as stale state that reap may clear.
        return manifest

    deadline = time.monotonic() + _REAP_RESERVATION_HANDSHAKE_SECONDS
    current = manifest
    while _runner_launch_identity_pending(current):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _invalid_state(
                "Runner launch reservation did not resolve before reap deadline",
                task_id=task_dir.name,
            )
        if _owned_process(current, "supervisor", task_dir) is None:
            raise _invalid_state(
                "Supervisor exited before recording the reserved runner identity",
                task_id=task_dir.name,
            )
        time.sleep(min(0.05, remaining))
        current = store.read_manifest(task_dir)
    return current


def reap_task(
    task_id: str,
    state_root: Path | None = None,
) -> dict[str, object]:
    store, task_dir, manifest = _manifest_for_task(task_id, state_root)
    manifest = _wait_for_reapable_manifest(store, task_dir, manifest)
    # Preflight every recorded identity before terminating either process. This
    # prevents partial cleanup when one recorded PID has been recycled.
    owned = {
        role: _owned_process(manifest, role, task_dir)
        for role in ("runner", "supervisor")
    }
    for role in ("runner", "supervisor"):
        process = owned[role]
        if process is None:
            continue
        identity, nonce = process
        try:
            terminate_owned_tree(
                identity.pid,
                identity.start_marker,
                nonce,
                _REAP_GRACE_SECONDS,
            )
        except WorkerError as error:
            if error.code != "process_not_found":
                raise
    def mark_reaped(current: dict[str, object]) -> dict[str, object]:
        now = utc_now()
        current.update(
            {
                "status": "reaped",
                "updated_at": now,
                "completed_at": now,
                "error": "Task processes were reaped by the controller",
                "runner_launching": False,
            }
        )
        return current

    return store.update_manifest(task_dir, mark_reaped)


def list_tasks(state_root: Path | None = None) -> dict[str, object]:
    store = _store(state_root)
    tasks = [
        _reconcile(store, store.task_dir(str(manifest["task_id"])), manifest)
        for manifest in store.list_manifests()
    ]
    tasks.sort(key=lambda value: str(value.get("task_id", "")))
    return {"tasks": tasks}


def _parse_timestamp(value: object, task_id: object) -> datetime:
    if not isinstance(value, str):
        raise _invalid_state(
            "Task updated_at timestamp is invalid",
            task_id=task_id,
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise _invalid_state(
            "Task updated_at timestamp is invalid",
            task_id=task_id,
        ) from error
    if parsed.tzinfo is None:
        raise _invalid_state(
            "Task updated_at timestamp must include a timezone",
            task_id=task_id,
        )
    return parsed.astimezone(timezone.utc)


def reap_stale_tasks(
    state_root: Path | None = None,
    older_than_sec: float = 3600.0,
    apply: bool = False,
) -> dict[str, object]:
    threshold = _finite_nonnegative(older_than_sec, "older_than_sec")
    if not isinstance(apply, bool):
        raise _invalid_arguments("apply must be a boolean", field="apply")
    now = datetime.now(timezone.utc)
    candidates: list[str] = []
    for manifest in _store(state_root).list_manifests():
        if manifest.get("status") in TERMINAL_STATES:
            continue
        updated = _parse_timestamp(manifest.get("updated_at"), manifest.get("task_id"))
        if (now - updated).total_seconds() >= threshold:
            candidates.append(str(manifest["task_id"]))
    candidates.sort()
    if apply:
        for task_id in candidates:
            reap_task(task_id, state_root)
    return {"apply": apply, "older_than_sec": threshold, "task_ids": candidates}


__all__ = [
    "cancel_task",
    "list_tasks",
    "reap_stale_tasks",
    "reap_task",
    "result_task",
    "route_task",
    "start_task",
    "status_task",
    "wait_task",
]
