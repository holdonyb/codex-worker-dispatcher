"""Owned-worker process identity and termination primitives.

Linux pidfds and Windows process handles provide stable references while an
owned process is inspected and signalled. macOS has no equivalent in the
Python standard library, so repeated adjacent identity/PGID checks minimize,
but cannot completely eliminate, the PID-reuse window there.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import math
import os
from pathlib import Path
import re
import select
import shlex
import signal
import subprocess
import sys
import time

from codex_worker_dispatcher.errors import WorkerError


_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)
_SYSTEM_COMMAND_TIMEOUT_SECONDS = 10.0
_WINDOWS_CREATE_NO_WINDOW = getattr(
    subprocess,
    "CREATE_NO_WINDOW",
    0x08000000,
)
_WINDOWS_CREATE_NEW_PROCESS_GROUP = getattr(
    subprocess,
    "CREATE_NEW_PROCESS_GROUP",
    0x00000200,
)


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    pid: int
    start_marker: str
    command_line: str


@dataclass(frozen=True, slots=True)
class _LinuxProcessGroupMember:
    pid: int
    process_group: int
    start_marker: str


@dataclass(frozen=True, slots=True)
class _LinuxPidfdAnchor:
    pid: int
    file_descriptor: int

    def is_signaled(self) -> bool:
        try:
            return _pidfd_is_signaled(self.file_descriptor)
        except OSError as error:
            raise _invalid_query(
                self.pid,
                "Could not poll Linux process anchor",
                error=str(error),
            ) from error


@dataclass(frozen=True, slots=True)
class _UnstableProcessAnchor:
    pid: int

    def is_signaled(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class _WindowsProcessAnchor:
    pid: int
    handle: object

    def is_signaled(self) -> bool:
        return _windows_handle_is_signaled(self.pid, self.handle)


class _LinuxProcessAnchorContext:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.file_descriptor: int | None = None

    def __enter__(self) -> _LinuxPidfdAnchor | _UnstableProcessAnchor:
        pidfd_open = getattr(os, "pidfd_open", None)
        if pidfd_open is None:
            return _UnstableProcessAnchor(self.pid)
        try:
            self.file_descriptor = pidfd_open(self.pid, 0)
        except (ProcessLookupError, FileNotFoundError) as error:
            raise _process_not_found(self.pid) from error
        except OSError as error:
            if error.errno == errno.ENOSYS:
                return _UnstableProcessAnchor(self.pid)
            raise _invalid_query(
                self.pid,
                "Could not open Linux process anchor",
                errno=error.errno,
                error=str(error),
            ) from error
        return _LinuxPidfdAnchor(self.pid, self.file_descriptor)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if self.file_descriptor is None:
            return False
        try:
            os.close(self.file_descriptor)
        except OSError as error:
            if exc_type is None:
                raise _invalid_query(
                    self.pid,
                    "Could not close Linux process anchor",
                    error=str(error),
                ) from error
        return False


class _UnstableProcessAnchorContext:
    def __init__(self, pid: int) -> None:
        self.anchor = _UnstableProcessAnchor(pid)

    def __enter__(self) -> _UnstableProcessAnchor:
        return self.anchor

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        return False


class _WindowsProcessAnchorContext:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.handle: object | None = None

    def __enter__(self) -> _WindowsProcessAnchor:
        self.handle = _windows_open_process_handle(self.pid)
        return _WindowsProcessAnchor(self.pid, self.handle)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        if self.handle is None:
            return False
        try:
            _windows_close_process_handle(self.pid, self.handle)
        except WorkerError:
            if exc_type is None:
                raise
        return False


def ownership_hash(nonce: str) -> str:
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


def windows_no_window_flags() -> int:
    if sys.platform != "win32":
        return 0
    return _WINDOWS_CREATE_NO_WINDOW


def windows_detached_flags() -> int:
    if sys.platform != "win32":
        return 0
    return windows_no_window_flags() | _WINDOWS_CREATE_NEW_PROCESS_GROUP


def read_process_identity(pid: int) -> ProcessIdentity:
    pid = _validate_pid(pid)
    if sys.platform == "win32":
        return _read_windows_identity(pid)
    if sys.platform == "linux":
        return _read_linux_identity(pid)
    if sys.platform == "darwin":
        return _read_macos_identity(pid)
    raise WorkerError(
        "invalid_state",
        "Process identity inspection is not supported on this platform",
        {"pid": pid, "platform": sys.platform},
    )


def identity_matches(
    identity: ProcessIdentity,
    expected_start_marker: str,
    ownership_nonce: str,
) -> bool:
    if not ownership_nonce or identity.start_marker != expected_start_marker:
        return False
    return ownership_nonce in _command_tokens(identity.command_line)


def owned_task_identity_matches(
    identity: ProcessIdentity,
    expected_start_marker: str,
    ownership_nonce: str,
    role: str,
    task_dir: Path,
) -> bool:
    """Match one exact dispatcher role, task directory, nonce, and start ID."""

    if role not in {"runner", "supervisor"} or not identity_matches(
        identity,
        expected_start_marker,
        ownership_nonce,
    ):
        return False
    try:
        tokens = _command_tokens(identity.command_line)
        module = f"codex_worker_dispatcher.{role}"
        module_flags = [
            index for index, token in enumerate(tokens) if token == "-m"
        ]
        module_positions = [
            index
            for index in range(len(tokens) - 1)
            if tokens[index] == "-m" and tokens[index + 1] == module
        ]
        task_positions = [
            index
            for index, token in enumerate(tokens)
            if token == "--task-dir"
        ]
        nonce_positions = [
            index
            for index, token in enumerate(tokens)
            if token == "--ownership-nonce"
        ]
        if (
            any(
                token.startswith("--task-dir=")
                or token.startswith("--ownership-nonce=")
                for token in tokens
            )
            or len(module_flags) != 1
            or len(module_positions) != 1
            or len(task_positions) != 1
            or len(nonce_positions) != 1
            or task_positions[0] + 1 >= len(tokens)
            or nonce_positions[0] + 1 >= len(tokens)
            or tokens[nonce_positions[0] + 1] != ownership_nonce
        ):
            return False
        expected_task_dir = os.path.normcase(
            os.path.normpath(os.path.abspath(os.fspath(Path(task_dir))))
        )
        actual_task_dir = os.path.normcase(
            os.path.normpath(
                os.path.abspath(tokens[task_positions[0] + 1])
            )
        )
        return actual_task_dir == expected_task_dir
    except (OSError, RuntimeError, ValueError):
        return False


def wait_until_gone(pid: int, timeout_seconds: float) -> bool:
    pid = _validate_pid(pid)
    timeout = _nonnegative_seconds(timeout_seconds, "timeout_seconds")
    deadline = time.monotonic() + timeout
    while True:
        if _process_is_gone(pid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def terminate_owned_tree(
    pid: int,
    expected_start_marker: str,
    ownership_nonce: str,
    grace_seconds: float = 3.0,
) -> None:
    pid = _validate_pid(pid)
    grace = _nonnegative_seconds(grace_seconds, "grace_seconds")
    if sys.platform == "win32":
        _terminate_windows_tree(pid, expected_start_marker, ownership_nonce, grace)
        return
    if sys.platform in {"linux", "darwin"}:
        _terminate_posix_group(pid, expected_start_marker, ownership_nonce, grace)
        return
    raise WorkerError(
        "invalid_state",
        "Process termination is not supported on this platform",
        {"pid": pid, "platform": sys.platform},
    )


def _validate_pid(pid: int) -> int:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise WorkerError(
            "invalid_arguments",
            "PID must be a positive integer",
            {"pid": pid},
        )
    return pid


def _nonnegative_seconds(value: float, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        valid = False
    else:
        try:
            seconds = float(value)
        except OverflowError:
            valid = False
        else:
            valid = math.isfinite(seconds) and seconds >= 0
    if not valid:
        raise WorkerError(
            "invalid_arguments",
            f"{field} must be a finite non-negative number",
            {"field": field, "value": value},
        )
    return seconds


def _process_not_found(pid: int) -> WorkerError:
    return WorkerError(
        "process_not_found",
        f"Process not found: {pid}",
        {"pid": pid},
    )


def _invalid_query(
    pid: int,
    message: str,
    **details: object,
) -> WorkerError:
    return WorkerError(
        "invalid_state",
        message,
        {"pid": pid, "platform": sys.platform, **details},
    )


def _pidfd_is_signaled(file_descriptor: int) -> bool:
    poller_factory = getattr(select, "poll", None)
    if poller_factory is None:
        raise OSError(errno.ENOSYS, "poll is unavailable")
    poll_in = getattr(select, "POLLIN", 0x001)
    poll_error = getattr(select, "POLLERR", 0x008)
    poll_hangup = getattr(select, "POLLHUP", 0x010)
    poll_invalid = getattr(select, "POLLNVAL", 0x020)
    poller = poller_factory()
    poller.register(file_descriptor, poll_in | poll_error | poll_hangup)
    events = poller.poll(0)
    if any(event & poll_invalid for _, event in events):
        raise OSError(errno.EBADF, "invalid pidfd")
    return bool(events)


def _send_linux_pidfd_signal(
    anchor: _LinuxPidfdAnchor,
    signal_number: int,
) -> bool:
    sender = getattr(signal, "pidfd_send_signal", None)
    if not callable(sender):
        raise _invalid_query(
            anchor.pid,
            "Linux pidfd signaling is unavailable",
            signal=int(signal_number),
        )
    try:
        sender(anchor.file_descriptor, signal_number, None, 0)
    except ProcessLookupError:
        return False
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        message = "Could not signal anchored Linux process"
        if error.errno == errno.ENOSYS:
            message = "Linux pidfd signaling is unavailable"
        raise _invalid_query(
            anchor.pid,
            message,
            signal=int(signal_number),
            errno=error.errno,
            error=str(error),
        ) from error
    return True


def _linux_process_anchor(
    pid: int,
) -> _LinuxProcessAnchorContext:
    return _LinuxProcessAnchorContext(pid)


def _windows_open_process_handle(pid: int) -> object:
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    process_query_limited_information = 0x00001000
    error_access_denied = 5
    error_invalid_parameter = 87
    error_not_found = 1168
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(
        synchronize | process_query_limited_information,
        False,
        pid,
    )
    if handle:
        return handle
    error_code = ctypes.get_last_error()
    if error_code in {error_invalid_parameter, error_not_found}:
        raise _process_not_found(pid)
    message = "Access denied while opening Windows process anchor"
    if error_code != error_access_denied:
        message = "Could not open Windows process anchor"
    raise _invalid_query(pid, message, winerror=error_code)


def _windows_handle_is_signaled(pid: int, handle: object) -> bool:
    import ctypes
    from ctypes import wintypes

    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    result = kernel32.WaitForSingleObject(handle, 0)
    if result == wait_object_0:
        return True
    if result == wait_timeout:
        return False
    error_code = ctypes.get_last_error() if result == wait_failed else None
    raise _invalid_query(
        pid,
        "Could not poll Windows process anchor",
        wait_result=result,
        winerror=error_code,
    )


def _windows_close_process_handle(pid: int, handle: object) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(handle):
        raise _invalid_query(
            pid,
            "Could not close Windows process anchor",
            winerror=ctypes.get_last_error(),
        )


def _windows_process_anchor(pid: int) -> _WindowsProcessAnchorContext:
    return _WindowsProcessAnchorContext(pid)


def _posix_process_anchor(
    pid: int,
) -> _LinuxProcessAnchorContext | _UnstableProcessAnchorContext:
    if sys.platform == "linux":
        return _linux_process_anchor(pid)
    return _UnstableProcessAnchorContext(pid)


def _require_anchor_alive(
    pid: int,
    anchor: _LinuxPidfdAnchor | _UnstableProcessAnchor | _WindowsProcessAnchor,
) -> None:
    if anchor.is_signaled():
        raise _process_not_found(pid)


def _read_windows_identity(pid: int) -> ProcessIdentity:
    script = (
        "$ErrorActionPreference='Stop';"
        "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8;"
        f"$process=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}';"
        "if ($null -eq $process) { exit 3 };"
        "if ([string]::IsNullOrEmpty($process.CreationDate) -or "
        "[string]::IsNullOrEmpty($process.CommandLine)) {"
        f"$process=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}';"
        "if ($null -eq $process) { exit 3 }"
        "};"
        "$process | Select-Object CreationDate,CommandLine | "
        "ConvertTo-Json -Compress"
    )
    for attempt in range(3):
        try:
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
                creationflags=windows_no_window_flags(),
            )
        except subprocess.TimeoutExpired as error:
            raise _invalid_query(
                pid,
                "PowerShell process inspection timed out",
                timeout_seconds=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
                command=error.cmd,
            ) from error
        except OSError as error:
            raise _invalid_query(
                pid,
                "Could not invoke PowerShell for process inspection",
                error=str(error),
            ) from error
        if result.returncode == 3:
            raise _process_not_found(pid)
        if result.returncode != 0:
            raise _invalid_query(
                pid,
                "PowerShell process inspection failed",
                returncode=result.returncode,
                stderr=result.stderr.strip(),
            )
        try:
            return _parse_windows_identity(pid, result.stdout)
        except WorkerError as error:
            if (
                error.message
                != "PowerShell process identity is missing required fields"
                or attempt == 2
            ):
                raise
            time.sleep(0.01)
    raise AssertionError("unreachable")


def _parse_windows_identity(pid: int, payload: str) -> ProcessIdentity:
    try:
        parsed = json.loads(payload.lstrip("\ufeff\r\n "))
    except (json.JSONDecodeError, TypeError) as error:
        raise _invalid_query(
            pid,
            "PowerShell returned invalid process identity JSON",
            error=str(error),
        ) from error
    if not isinstance(parsed, dict):
        raise _invalid_query(pid, "PowerShell returned an invalid process identity")
    start_marker = parsed.get("CreationDate")
    command_line = parsed.get("CommandLine")
    if (
        not isinstance(start_marker, str)
        or not start_marker
        or not isinstance(command_line, str)
        or not command_line
    ):
        raise _invalid_query(
            pid,
            "PowerShell process identity is missing required fields",
        )
    return ProcessIdentity(pid, start_marker, command_line)


def _parse_linux_stat_fields(stat_line: str) -> tuple[str, int, str]:
    closing_parenthesis = stat_line.rfind(")")
    if closing_parenthesis < 0:
        raise ValueError("missing process command terminator")
    fields = stat_line[closing_parenthesis + 1 :].split()
    if len(fields) <= 19:
        raise ValueError("process stat does not contain field 22")
    try:
        process_group = int(fields[2])
    except ValueError as error:
        raise ValueError("process stat contains an invalid process group") from error
    return fields[0], process_group, fields[19]


def _parse_linux_stat(stat_line: str) -> tuple[str, str]:
    state, _, start_marker = _parse_linux_stat_fields(stat_line)
    return state, start_marker


def _read_linux_stat(pid: int) -> tuple[str, str]:
    path = Path("/proc") / str(pid) / "stat"
    try:
        content = path.read_text(encoding="utf-8", errors="surrogateescape")
    except (FileNotFoundError, ProcessLookupError) as error:
        raise _process_not_found(pid) from error
    except OSError as error:
        raise _invalid_query(
            pid,
            "Could not read Linux process stat",
            path=str(path),
            error=str(error),
        ) from error
    try:
        return _parse_linux_stat(content)
    except ValueError as error:
        raise _invalid_query(
            pid,
            "Linux process stat is invalid",
            path=str(path),
            error=str(error),
        ) from error


def _read_linux_identity(pid: int) -> ProcessIdentity:
    state, start_marker = _read_linux_stat(pid)
    if state == "Z":
        raise _process_not_found(pid)
    command_path = Path("/proc") / str(pid) / "cmdline"
    try:
        command_bytes = command_path.read_bytes()
    except (FileNotFoundError, ProcessLookupError) as error:
        raise _process_not_found(pid) from error
    except OSError as error:
        raise _invalid_query(
            pid,
            "Could not read Linux process command line",
            path=str(command_path),
            error=str(error),
        ) from error
    final_state, final_start_marker = _read_linux_stat(pid)
    if final_state == "Z" or final_start_marker != start_marker:
        raise _process_not_found(pid)
    command_line = command_bytes.rstrip(b"\0").decode(
        errors="surrogateescape"
    )
    if not command_line:
        raise _invalid_query(pid, "Linux process command line is empty")
    return ProcessIdentity(pid, start_marker, command_line)


_MACOS_IDENTITY_PATTERN = re.compile(
    r"^\s*([A-Za-z]{3}\s+[A-Za-z]{3}\s+\d{1,2}\s+"
    r"\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.+?)\s*$",
    re.DOTALL,
)


def _read_macos_identity(pid: int) -> ProcessIdentity:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "command="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            timeout=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise _invalid_query(
            pid,
            "ps process inspection timed out",
            timeout_seconds=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
            command=error.cmd,
        ) from error
    except OSError as error:
        raise _invalid_query(
            pid,
            "Could not invoke ps for process inspection",
            error=str(error),
        ) from error
    if result.returncode == 1 and not result.stdout.strip():
        raise _process_not_found(pid)
    if result.returncode != 0:
        raise _invalid_query(
            pid,
            "ps process inspection failed",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )
    return _parse_macos_identity(pid, result.stdout)


def _parse_macos_identity(pid: int, output: str) -> ProcessIdentity:
    match = _MACOS_IDENTITY_PATTERN.match(output)
    if match is None:
        raise _invalid_query(pid, "ps returned an invalid process identity")
    return ProcessIdentity(pid, match.group(1), match.group(2))


def _command_tokens(command_line: str) -> tuple[str, ...]:
    if "\0" in command_line:
        return tuple(token for token in command_line.split("\0") if token)
    try:
        if sys.platform == "win32":
            return _windows_command_tokens(command_line)
        return tuple(shlex.split(command_line, posix=True))
    except (OSError, ValueError):
        return ()


def _windows_command_tokens(command_line: str) -> tuple[str, ...]:
    import ctypes
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    command_line_to_argv = shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    command_line_to_argv.restype = ctypes.POINTER(wintypes.LPWSTR)
    argument_count = ctypes.c_int()
    arguments = command_line_to_argv(command_line, ctypes.byref(argument_count))
    if not arguments:
        raise OSError(ctypes.get_last_error(), "CommandLineToArgvW failed")
    try:
        return tuple(arguments[index] for index in range(argument_count.value))
    finally:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL
        kernel32.LocalFree(arguments)


def _process_is_gone(pid: int) -> bool:
    if sys.platform == "win32":
        return _windows_process_is_gone(pid)
    if sys.platform == "linux":
        try:
            state, _ = _read_linux_stat(pid)
        except WorkerError as error:
            if error.code == "process_not_found":
                return True
            raise
        return state == "Z"
    if sys.platform == "darwin":
        return _macos_process_is_gone(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


def _windows_process_is_gone(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    error_invalid_parameter = 87
    error_not_found = 1168
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error in {error_invalid_parameter, error_not_found}:
            return True
        return False
    try:
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        return kernel32.WaitForSingleObject(handle, 0) == wait_object_0
    finally:
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(handle)


def _macos_process_is_gone(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "stat="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            timeout=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise _invalid_query(
            pid,
            "ps timed out while waiting for process exit",
            timeout_seconds=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
            command=error.cmd,
        ) from error
    except OSError as error:
        raise _invalid_query(
            pid,
            "Could not invoke ps while waiting for process exit",
            error=str(error),
        ) from error
    if result.returncode == 1 and not result.stdout.strip():
        return True
    if result.returncode != 0:
        raise _invalid_query(
            pid,
            "ps failed while waiting for process exit",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )
    status = result.stdout.strip()
    return not status or status.startswith("Z")


def _verified_identity(
    pid: int,
    expected_start_marker: str,
    ownership_nonce: str,
) -> ProcessIdentity:
    identity = read_process_identity(pid)
    if identity.pid != pid or not identity_matches(
        identity,
        expected_start_marker,
        ownership_nonce,
    ):
        raise WorkerError(
            "process_identity_mismatch",
            f"Process identity does not match the owned worker: {pid}",
            {
                "pid": pid,
                "expected_start_marker": expected_start_marker,
                "actual_start_marker": identity.start_marker,
            },
        )
    return identity


def _verified_posix_group(
    pid: int,
    expected_start_marker: str,
    ownership_nonce: str,
) -> None:
    _verified_identity(pid, expected_start_marker, ownership_nonce)
    try:
        process_group = _get_process_group(pid)
    except ProcessLookupError as error:
        raise _process_not_found(pid) from error
    except OSError as error:
        raise _invalid_query(
            pid,
            "Could not inspect the worker process group",
            error=str(error),
        ) from error
    if process_group != pid:
        raise WorkerError(
            "process_identity_mismatch",
            f"Worker is not the leader of its process group: {pid}",
            {"pid": pid, "expected_pgid": pid, "actual_pgid": process_group},
        )
    _verified_identity(pid, expected_start_marker, ownership_nonce)


def _get_process_group(pid: int) -> int:
    return os.getpgid(pid)


def _process_state_is_live(state: str) -> bool:
    return bool(state) and state[0].upper() not in {"X", "Z"}


def _linux_process_group_members(
    process_group: int,
    proc_root: Path = Path("/proc"),
) -> tuple[_LinuxProcessGroupMember, ...]:
    try:
        process_directories = tuple(proc_root.iterdir())
    except OSError as error:
        raise _invalid_query(
            process_group,
            "Could not enumerate Linux processes",
            path=str(proc_root),
            error=str(error),
        ) from error
    members: list[_LinuxProcessGroupMember] = []
    for process_directory in process_directories:
        if not process_directory.name.isdecimal():
            continue
        stat_path = process_directory / "stat"
        try:
            stat_line = stat_path.read_text(
                encoding="utf-8",
                errors="surrogateescape",
            )
        except (FileNotFoundError, ProcessLookupError):
            continue
        except OSError as error:
            raise _invalid_query(
                process_group,
                "Could not inspect a Linux process group member",
                path=str(stat_path),
                error=str(error),
            ) from error
        try:
            state, member_process_group, start_marker = _parse_linux_stat_fields(
                stat_line
            )
        except ValueError as error:
            raise _invalid_query(
                process_group,
                "Linux process stat is invalid during group inspection",
                path=str(stat_path),
                error=str(error),
            ) from error
        if (
            member_process_group == process_group
            and _process_state_is_live(state)
        ):
            members.append(
                _LinuxProcessGroupMember(
                    pid=int(process_directory.name),
                    process_group=member_process_group,
                    start_marker=start_marker,
                )
            )
    return tuple(sorted(members, key=lambda member: member.pid))


def _read_linux_process_group_member(
    pid: int,
    proc_root: Path = Path("/proc"),
) -> _LinuxProcessGroupMember | None:
    stat_path = proc_root / str(pid) / "stat"
    try:
        stat_line = stat_path.read_text(
            encoding="utf-8",
            errors="surrogateescape",
        )
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as error:
        raise _invalid_query(
            pid,
            "Could not inspect Linux process group member identity",
            path=str(stat_path),
            error=str(error),
        ) from error
    try:
        state, process_group, start_marker = _parse_linux_stat_fields(stat_line)
    except ValueError as error:
        raise _invalid_query(
            pid,
            "Linux process stat is invalid during member verification",
            path=str(stat_path),
            error=str(error),
        ) from error
    if not _process_state_is_live(state):
        return None
    return _LinuxProcessGroupMember(pid, process_group, start_marker)


def _close_linux_member_anchor(anchor: _LinuxPidfdAnchor) -> None:
    try:
        os.close(anchor.file_descriptor)
    except OSError as error:
        raise _invalid_query(
            anchor.pid,
            "Could not close Linux process group member anchor",
            error=str(error),
        ) from error


def _open_verified_linux_member_anchor(
    member: _LinuxProcessGroupMember,
) -> _LinuxPidfdAnchor | None:
    pidfd_open = getattr(os, "pidfd_open", None)
    if not callable(pidfd_open):
        raise _invalid_query(
            member.pid,
            "Linux pidfd process anchoring is unavailable",
        )
    try:
        file_descriptor = pidfd_open(member.pid, 0)
    except (ProcessLookupError, FileNotFoundError):
        return None
    except OSError as error:
        if error.errno == errno.ESRCH:
            return None
        message = "Could not open Linux process group member anchor"
        if error.errno == errno.ENOSYS:
            message = "Linux pidfd process anchoring is unavailable"
        raise _invalid_query(
            member.pid,
            message,
            errno=error.errno,
            error=str(error),
        ) from error
    anchor = _LinuxPidfdAnchor(member.pid, file_descriptor)
    close_required = True
    try:
        if anchor.is_signaled():
            close_required = False
            _close_linux_member_anchor(anchor)
            return None
        current_member = _read_linux_process_group_member(member.pid)
        if current_member != member or anchor.is_signaled():
            close_required = False
            _close_linux_member_anchor(anchor)
            return None
    except BaseException:
        if close_required:
            try:
                _close_linux_member_anchor(anchor)
            except WorkerError:
                pass
        raise
    return anchor


class _LinuxOwnedProcessGroup:
    def __init__(
        self,
        leader_member: _LinuxProcessGroupMember,
        leader_anchor: _LinuxPidfdAnchor,
    ) -> None:
        self.process_group = leader_member.process_group
        self.leader_member = leader_member
        self.leader_anchor = leader_anchor
        self._anchors: dict[tuple[int, str], _LinuxPidfdAnchor] = {
            self._key(leader_member): leader_anchor
        }
        self._owned_keys: set[tuple[int, str]] = set()
        self._exited_file_descriptors: set[int] = set()

    @staticmethod
    def _key(member: _LinuxProcessGroupMember) -> tuple[int, str]:
        return member.pid, member.start_marker

    def __enter__(self) -> _LinuxOwnedProcessGroup:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        close_error: WorkerError | None = None
        for key in tuple(self._owned_keys):
            anchor = self._anchors.pop(key)
            try:
                _close_linux_member_anchor(anchor)
            except WorkerError as error:
                if close_error is None:
                    close_error = error
        self._owned_keys.clear()
        if close_error is not None and exc_type is None:
            raise close_error
        return False

    def ordered_anchors(self) -> tuple[_LinuxPidfdAnchor, ...]:
        return tuple(
            sorted(
                self._anchors.values(),
                key=lambda anchor: (
                    anchor.pid == self.leader_member.pid,
                    anchor.pid,
                ),
            )
        )

    def anchor_exited(self, anchor: _LinuxPidfdAnchor) -> bool:
        return (
            anchor.file_descriptor in self._exited_file_descriptors
            or anchor.is_signaled()
        )

    def leader_exited(self) -> bool:
        return self.anchor_exited(self.leader_anchor)

    def has_live_anchors(self) -> bool:
        return any(not self.anchor_exited(anchor) for anchor in self._anchors.values())

    def signal_anchors(
        self,
        anchors: tuple[_LinuxPidfdAnchor, ...],
        signal_number: int,
    ) -> None:
        for anchor in anchors:
            if self.anchor_exited(anchor):
                self._exited_file_descriptors.add(anchor.file_descriptor)
                continue
            if not _send_linux_pidfd_signal(anchor, signal_number):
                self._exited_file_descriptors.add(anchor.file_descriptor)

    def refresh(
        self,
    ) -> tuple[
        tuple[_LinuxProcessGroupMember, ...],
        tuple[_LinuxPidfdAnchor, ...],
        tuple[_LinuxProcessGroupMember, ...],
    ]:
        members = _linux_process_group_members(self.process_group)
        unknown = tuple(
            member for member in members if self._key(member) not in self._anchors
        )
        if not unknown:
            return members, (), ()
        if self.leader_exited():
            raise _invalid_query(
                self.process_group,
                "Cannot safely reclaim Linux process group after leader exit",
                unknown_pids=[member.pid for member in unknown],
            )
        candidates: list[tuple[_LinuxProcessGroupMember, _LinuxPidfdAnchor]] = []
        try:
            for member in unknown:
                if self.leader_exited():
                    raise _invalid_query(
                        self.process_group,
                        "Cannot safely anchor new Linux group member after leader exit",
                        unknown_pid=member.pid,
                    )
                anchor = _open_verified_linux_member_anchor(member)
                if anchor is not None:
                    candidates.append((member, anchor))
            if self.leader_exited():
                raise _invalid_query(
                    self.process_group,
                    "Cannot safely anchor Linux group members after leader exit",
                    unknown_pids=[member.pid for member, _ in candidates],
                )
        except BaseException:
            for _, anchor in candidates:
                try:
                    _close_linux_member_anchor(anchor)
                except WorkerError:
                    pass
            raise
        for member, anchor in candidates:
            key = self._key(member)
            self._anchors[key] = anchor
            self._owned_keys.add(key)
        unresolved = tuple(
            member for member in unknown if self._key(member) not in self._anchors
        )
        return members, tuple(anchor for _, anchor in candidates), unresolved


def _require_linux_pidfd_termination(
    pid: int,
    anchor: _LinuxPidfdAnchor | _UnstableProcessAnchor,
) -> _LinuxPidfdAnchor:
    if not isinstance(anchor, _LinuxPidfdAnchor):
        raise _invalid_query(
            pid,
            "Linux pidfd process anchoring is required for safe termination",
        )
    if not callable(getattr(signal, "pidfd_send_signal", None)):
        raise _invalid_query(
            pid,
            "Linux pidfd signaling is required for safe termination",
        )
    return anchor


def _stabilize_linux_owned_group(
    group: _LinuxOwnedProcessGroup,
    *,
    new_member_signal: int | None = None,
) -> None:
    deadline = time.monotonic() + _SYSTEM_COMMAND_TIMEOUT_SECONDS
    previous_keys: tuple[tuple[int, str], ...] | None = None
    stable_snapshots = 0
    while stable_snapshots < 2:
        if group.leader_exited():
            raise _invalid_query(
                group.process_group,
                "Cannot safely stabilize Linux process group after leader exit",
            )
        members, new_anchors, unresolved = group.refresh()
        if new_member_signal is not None and new_anchors:
            group.signal_anchors(new_anchors, new_member_signal)
        if group.leader_exited():
            raise _invalid_query(
                group.process_group,
                "Cannot safely stabilize Linux process group after leader exit",
            )
        member_keys = tuple(
            (member.pid, member.start_marker) for member in members
        )
        if not unresolved and member_keys == previous_keys:
            stable_snapshots += 1
        elif not unresolved:
            stable_snapshots = 1
        else:
            stable_snapshots = 0
        previous_keys = member_keys if not unresolved else None
        if stable_snapshots >= 2:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _invalid_query(
                group.process_group,
                "Could not obtain a stable Linux process group snapshot",
                unresolved_pids=[member.pid for member in unresolved],
            )
        time.sleep(min(0.01, remaining))


def _wait_until_linux_owned_group_gone(
    group: _LinuxOwnedProcessGroup,
    timeout_seconds: float,
    *,
    new_member_signal: int,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    empty_snapshots = 0
    while True:
        members, new_anchors, unresolved = group.refresh()
        if new_anchors:
            group.signal_anchors(new_anchors, new_member_signal)
        if not members and not unresolved and not group.has_live_anchors():
            empty_snapshots += 1
            if empty_snapshots >= 2:
                return True
        else:
            empty_snapshots = 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.01, remaining))


def _terminate_linux_owned_group(
    pid: int,
    expected_start_marker: str,
    ownership_nonce: str,
    grace_seconds: float,
    leader_anchor: _LinuxPidfdAnchor,
) -> None:
    leader_member = _LinuxProcessGroupMember(
        pid,
        pid,
        expected_start_marker,
    )
    with _LinuxOwnedProcessGroup(leader_member, leader_anchor) as group:
        _stabilize_linux_owned_group(group)
        _require_anchor_alive(pid, leader_anchor)
        _verified_posix_group(pid, expected_start_marker, ownership_nonce)
        _require_anchor_alive(pid, leader_anchor)

        descendants = tuple(
            anchor
            for anchor in group.ordered_anchors()
            if anchor.pid != pid
        )
        group.signal_anchors(descendants, _SIGTERM)
        _stabilize_linux_owned_group(
            group,
            new_member_signal=_SIGTERM,
        )
        group.signal_anchors((leader_anchor,), _SIGTERM)

        if _wait_until_linux_owned_group_gone(
            group,
            grace_seconds,
            new_member_signal=_SIGTERM,
        ):
            return

        group.signal_anchors(group.ordered_anchors(), _SIGKILL)
        if not _wait_until_linux_owned_group_gone(
            group,
            max(grace_seconds, 1.0),
            new_member_signal=_SIGKILL,
        ):
            raise _invalid_query(pid, "Owned worker process group did not exit")


def _linux_process_group_has_live_members(
    process_group: int,
    proc_root: Path = Path("/proc"),
) -> bool:
    return bool(_linux_process_group_members(process_group, proc_root))


def _parse_macos_process_group_has_live_members(
    process_group: int,
    output: str,
) -> bool:
    for line in output.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 3:
            raise _invalid_query(
                process_group,
                "ps returned an invalid process group snapshot",
                line=line,
            )
        try:
            int(fields[0])
            member_process_group = int(fields[1])
        except ValueError as error:
            raise _invalid_query(
                process_group,
                "ps returned an invalid process group member",
                line=line,
            ) from error
        if (
            member_process_group == process_group
            and _process_state_is_live(fields[2])
        ):
            return True
    return False


def _macos_process_group_has_live_members(process_group: int) -> bool:
    arguments = [
        "ps",
        "-ww",
        "-A",
        "-o",
        "pid=",
        "-o",
        "pgid=",
        "-o",
        "stat=",
    ]
    try:
        result = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            env={**os.environ, "LC_ALL": "C"},
            timeout=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise _invalid_query(
            process_group,
            "ps process group inspection timed out",
            timeout_seconds=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
            command=error.cmd,
        ) from error
    except OSError as error:
        raise _invalid_query(
            process_group,
            "Could not invoke ps for process group inspection",
            error=str(error),
        ) from error
    if result.returncode != 0:
        raise _invalid_query(
            process_group,
            "ps process group inspection failed",
            returncode=result.returncode,
            stderr=result.stderr.strip(),
        )
    return _parse_macos_process_group_has_live_members(
        process_group,
        result.stdout,
    )


def _process_group_has_live_members(process_group: int) -> bool:
    if sys.platform == "linux":
        return _linux_process_group_has_live_members(process_group)
    if sys.platform == "darwin":
        return _macos_process_group_has_live_members(process_group)
    raise _invalid_query(
        process_group,
        f"Unsupported POSIX platform: {sys.platform}",
    )


def _process_group_exists(process_group: int) -> bool:
    try:
        os.kill(-process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        if error.errno == errno.ESRCH:
            return False
        raise _invalid_query(
            process_group,
            "Could not inspect owned worker process group",
            error=str(error),
        ) from error
    return True


def _process_group_is_gone(process_group: int) -> bool:
    if not _process_group_exists(process_group):
        return True
    for _ in range(2):
        if _process_group_has_live_members(process_group):
            return False
        if not _process_group_exists(process_group):
            return True
    return True


def _wait_until_process_group_gone(
    process_group: int,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _process_group_is_gone(process_group):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))


def _anchored_leader_is_gone(
    pid: int,
    anchor: _LinuxPidfdAnchor | _UnstableProcessAnchor,
) -> bool:
    if isinstance(anchor, _LinuxPidfdAnchor):
        return anchor.is_signaled()
    return _process_is_gone(pid)


def _terminate_posix_group(
    pid: int,
    expected_start_marker: str,
    ownership_nonce: str,
    grace_seconds: float,
) -> None:
    with _posix_process_anchor(pid) as anchor:
        _require_anchor_alive(pid, anchor)
        _verified_posix_group(pid, expected_start_marker, ownership_nonce)
        _require_anchor_alive(pid, anchor)
        _verified_posix_group(pid, expected_start_marker, ownership_nonce)
        _require_anchor_alive(pid, anchor)
        if sys.platform == "linux":
            linux_anchor = _require_linux_pidfd_termination(pid, anchor)
            _terminate_linux_owned_group(
                pid,
                expected_start_marker,
                ownership_nonce,
                grace_seconds,
                linux_anchor,
            )
            return
        try:
            os.kill(-pid, _SIGTERM)
        except ProcessLookupError:
            return
        except OSError as error:
            raise _invalid_query(
                pid,
                "Could not signal the owned worker process group",
                signal="SIGTERM",
                error=str(error),
            ) from error
        if _wait_until_process_group_gone(pid, grace_seconds):
            return
        if not _anchored_leader_is_gone(pid, anchor):
            try:
                _verified_posix_group(pid, expected_start_marker, ownership_nonce)
            except WorkerError as error:
                if error.code != "process_not_found" or not _anchored_leader_is_gone(
                    pid,
                    anchor,
                ):
                    raise
        try:
            os.kill(-pid, _SIGKILL)
        except ProcessLookupError:
            return
        except OSError as error:
            raise _invalid_query(
                pid,
                "Could not force the owned worker process group to exit",
                signal="SIGKILL",
                error=str(error),
            ) from error
        if not _wait_until_process_group_gone(
            pid,
            max(grace_seconds, 1.0),
        ):
            raise _invalid_query(pid, "Owned worker process group did not exit")


def _run_taskkill(pid: int, force: bool) -> subprocess.CompletedProcess[str]:
    arguments = ["taskkill", "/PID", str(pid), "/T"]
    if force:
        arguments.append("/F")
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
            creationflags=windows_no_window_flags(),
        )
    except subprocess.TimeoutExpired as error:
        raise _invalid_query(
            pid,
            "taskkill timed out for the owned worker",
            force=force,
            timeout_seconds=_SYSTEM_COMMAND_TIMEOUT_SECONDS,
            command=error.cmd,
        ) from error
    except OSError as error:
        raise _invalid_query(
            pid,
            "Could not invoke taskkill for the owned worker",
            force=force,
            error=str(error),
        ) from error


def _terminate_windows_tree(
    pid: int,
    expected_start_marker: str,
    ownership_nonce: str,
    grace_seconds: float,
) -> None:
    with _windows_process_anchor(pid) as anchor:
        _require_anchor_alive(pid, anchor)
        _verified_identity(pid, expected_start_marker, ownership_nonce)
        _require_anchor_alive(pid, anchor)
        _verified_identity(pid, expected_start_marker, ownership_nonce)
        _require_anchor_alive(pid, anchor)
        graceful_result = _run_taskkill(pid, force=False)
        if wait_until_gone(pid, grace_seconds):
            return
        _require_anchor_alive(pid, anchor)
        _verified_identity(pid, expected_start_marker, ownership_nonce)
        _require_anchor_alive(pid, anchor)
        _verified_identity(pid, expected_start_marker, ownership_nonce)
        _require_anchor_alive(pid, anchor)
        force_result = _run_taskkill(pid, force=True)
        if wait_until_gone(pid, max(grace_seconds, 1.0)):
            return
        raise _invalid_query(
            pid,
            "Owned worker process tree did not exit",
            graceful_returncode=graceful_result.returncode,
            graceful_stderr=graceful_result.stderr.strip(),
            force_returncode=force_result.returncode,
            force_stderr=force_result.stderr.strip(),
        )


__all__ = [
    "ProcessIdentity",
    "identity_matches",
    "owned_task_identity_matches",
    "ownership_hash",
    "read_process_identity",
    "terminate_owned_tree",
    "wait_until_gone",
    "windows_detached_flags",
    "windows_no_window_flags",
]
