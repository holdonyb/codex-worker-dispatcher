from dataclasses import FrozenInstanceError
import errno
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import call, patch

import codex_worker_dispatcher.process as process_module
from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.process import (
    ProcessIdentity,
    identity_matches,
    owned_task_identity_matches,
    ownership_hash,
    read_process_identity,
    terminate_owned_tree,
    wait_until_gone,
    windows_detached_flags,
)


class ProcessIdentityTests(unittest.TestCase):
    def test_process_identity_is_frozen(self) -> None:
        identity = ProcessIdentity(123, "start", "python worker.py")

        with self.assertRaises(FrozenInstanceError):
            identity.pid = 456  # type: ignore[misc]

    def test_ownership_hash_is_stable_sha256_hex(self) -> None:
        nonce = "worker_nonce-A9"

        self.assertEqual(
            ownership_hash(nonce),
            hashlib.sha256(nonce.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(ownership_hash(nonce), ownership_hash(nonce))

    def test_identity_requires_exact_start_marker_and_nonce_token(self) -> None:
        identity = ProcessIdentity(
            123,
            "start-1",
            "python\0-c\0pass\0ownership-nonce",
        )

        self.assertTrue(identity_matches(identity, "start-1", "ownership-nonce"))
        self.assertFalse(identity_matches(identity, "start-2", "ownership-nonce"))
        self.assertFalse(identity_matches(identity, "start-1", "other-nonce"))
        self.assertFalse(identity_matches(identity, "start-1", ""))

    def test_owned_task_identity_requires_exact_role_task_dir_and_nonce_flags(
        self,
    ) -> None:
        task_dir = Path(tempfile.gettempdir()) / "worker state" / "task-a"
        nonce = "0123456789abcdef0123456789abcdef"
        tokens = (
            sys.executable,
            "-m",
            "codex_worker_dispatcher.runner",
            "--task-dir",
            str(task_dir),
            "--ownership-nonce",
            nonce,
        )
        identity = ProcessIdentity(123, "start", "\0".join(tokens))

        self.assertTrue(
            owned_task_identity_matches(
                identity,
                "start",
                nonce,
                "runner",
                task_dir,
            )
        )
        for role, candidate_dir, candidate_nonce in (
            ("supervisor", task_dir, nonce),
            ("runner", task_dir.parent / "task-b", nonce),
            ("runner", task_dir, "fedcba9876543210fedcba9876543210"),
        ):
            with self.subTest(role=role, task_dir=candidate_dir):
                self.assertFalse(
                    owned_task_identity_matches(
                        identity,
                        "start",
                        candidate_nonce,
                        role,
                        candidate_dir,
                    )
                )

    def test_owned_task_identity_rejects_duplicate_or_unpaired_flags(self) -> None:
        task_dir = Path(tempfile.gettempdir()) / "task-a"
        nonce = "0123456789abcdef0123456789abcdef"
        for tokens in (
            (
                "python",
                "-m",
                "codex_worker_dispatcher.supervisor",
                "--task-dir",
                str(task_dir),
                "--task-dir",
                str(task_dir),
                "--ownership-nonce",
                nonce,
            ),
            (
                "python",
                "-m",
                "codex_worker_dispatcher.supervisor",
                str(task_dir),
                "--ownership-nonce",
                nonce,
            ),
            (
                "python",
                "-m",
                "codex_worker_dispatcher.supervisor",
                "--task-dir",
                str(task_dir),
                f"--task-dir={task_dir}",
                "--ownership-nonce",
                nonce,
            ),
        ):
            identity = ProcessIdentity(123, "start", "\0".join(tokens))
            self.assertFalse(
                owned_task_identity_matches(
                    identity,
                    "start",
                    nonce,
                    "supervisor",
                    task_dir,
                )
            )

    @unittest.skipUnless(sys.platform == "win32", "Windows command lines only")
    def test_owned_task_identity_parses_quoted_windows_task_path(self) -> None:
        task_dir = Path("C:/Worker State/task-a")
        nonce = "0123456789abcdef0123456789abcdef"
        command_line = (
            '"C:\\Program Files\\Python\\python.exe" '
            "-m codex_worker_dispatcher.supervisor "
            '--task-dir "C:\\Worker State\\task-a" '
            f"--ownership-nonce {nonce}"
        )
        identity = ProcessIdentity(123, "start", command_line)

        self.assertTrue(
            owned_task_identity_matches(
                identity,
                "start",
                nonce,
                "supervisor",
                task_dir,
            )
        )

    @unittest.skipUnless(
        sys.platform in {"linux", "darwin"},
        "POSIX command lines only",
    )
    def test_owned_task_identity_parses_quoted_posix_task_path(self) -> None:
        task_dir = Path(tempfile.gettempdir()) / "worker state" / "task-a"
        nonce = "0123456789abcdef0123456789abcdef"
        tokens = [
            sys.executable,
            "-m",
            "codex_worker_dispatcher.runner",
            "--task-dir",
            str(task_dir),
            "--ownership-nonce",
            nonce,
        ]
        identity = ProcessIdentity(123, "start", shlex.join(tokens))

        self.assertTrue(
            owned_task_identity_matches(
                identity,
                "start",
                nonce,
                "runner",
                task_dir,
            )
        )

    def test_identity_does_not_accept_nonce_substrings(self) -> None:
        for command_line in (
            "python worker.py xownership-noncex",
            "python worker.py ownership-nonce.extra",
            "python worker.py --token=ownership-nonce",
            'python -c "print(\'ownership-nonce\')"',
        ):
            with self.subTest(command_line=command_line):
                identity = ProcessIdentity(123, "start", command_line)
                self.assertFalse(
                    identity_matches(identity, "start", "ownership-nonce")
                )

    def test_identity_accepts_a_quoted_nonce_argument(self) -> None:
        identity = ProcessIdentity(
            123,
            "start",
            'python worker.py "quoted-nonce"',
        )

        self.assertTrue(identity_matches(identity, "start", "quoted-nonce"))

    def test_nul_command_line_identity_match_does_not_require_windows_api(self) -> None:
        identity = ProcessIdentity(
            123,
            "start",
            "python\0worker.py\0ownership-nonce",
        )
        with patch.object(process_module.sys, "platform", "win32"), patch.object(
            process_module,
            "_windows_command_tokens",
            side_effect=AssertionError("Windows API is unavailable"),
        ) as windows_command_tokens:
            self.assertTrue(
                identity_matches(identity, "start", "ownership-nonce")
            )

        windows_command_tokens.assert_not_called()

    def test_current_python_process_identity_can_be_read(self) -> None:
        identity = read_process_identity(os.getpid())

        self.assertEqual(identity.pid, os.getpid())
        self.assertTrue(identity.start_marker)
        self.assertTrue(identity.command_line)

    def test_missing_process_uses_stable_error_code(self) -> None:
        missing_pid = 2_147_483_647

        with self.assertRaises(WorkerError) as raised:
            read_process_identity(missing_pid)

        self.assertEqual(raised.exception.code, "process_not_found")
        self.assertEqual(raised.exception.details["pid"], missing_pid)

    def test_public_pid_arguments_must_be_positive_integers(self) -> None:
        operations = (
            lambda pid: read_process_identity(pid),
            lambda pid: wait_until_gone(pid, 0),
            lambda pid: terminate_owned_tree(pid, "start", "nonce", 0),
        )

        for invalid_pid in (0, -1, True, 1.5, "1"):
            for operation in operations:
                with self.subTest(pid=invalid_pid, operation=operation):
                    with self.assertRaises(WorkerError) as raised:
                        operation(invalid_pid)  # type: ignore[arg-type]
                    self.assertEqual(raised.exception.code, "invalid_arguments")

    def test_timeout_and_grace_must_be_finite_nonnegative_numbers(self) -> None:
        invalid_seconds = (float("nan"), float("inf"), float("-inf"), True)

        for value in invalid_seconds:
            with self.subTest(operation="wait", value=value), patch.object(
                process_module,
                "_process_is_gone",
                return_value=True,
            ):
                with self.assertRaises(WorkerError) as raised:
                    wait_until_gone(123, value)  # type: ignore[arg-type]
                self.assertEqual(raised.exception.code, "invalid_arguments")
            with self.subTest(operation="terminate", value=value), patch.object(
                process_module.sys,
                "platform",
                "win32",
            ), patch.object(process_module, "_terminate_windows_tree") as terminate:
                with self.assertRaises(WorkerError) as raised:
                    terminate_owned_tree(
                        123,
                        "start",
                        "nonce",
                        value,  # type: ignore[arg-type]
                    )
                self.assertEqual(raised.exception.code, "invalid_arguments")
                terminate.assert_not_called()

    def test_windows_detached_flags_are_platform_appropriate(self) -> None:
        expected = 0
        if sys.platform == "win32":
            expected = (
                subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )

        self.assertEqual(windows_detached_flags(), expected)
        if sys.platform == "win32":
            self.assertFalse(windows_detached_flags() & subprocess.DETACHED_PROCESS)

    def test_windows_no_window_flags_are_platform_appropriate(self) -> None:
        helper = getattr(process_module, "windows_no_window_flags", None)
        self.assertIsNotNone(helper)
        if helper is None:
            return
        expected = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        self.assertEqual(helper(), expected)

    @unittest.skipIf(sys.platform == "win32", "POSIX subprocess surface required")
    def test_simulated_windows_taskkill_uses_fallback_creation_flags(self) -> None:
        self.assertFalse(hasattr(subprocess, "CREATE_NO_WINDOW"))
        self.assertFalse(hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"))
        result = subprocess.CompletedProcess([], 0, "", "")

        with patch.object(process_module.sys, "platform", "win32"), patch.object(
            process_module.subprocess,
            "run",
            return_value=result,
        ) as run:
            self.assertEqual(process_module.windows_no_window_flags(), 0x08000000)
            self.assertEqual(windows_detached_flags(), 0x08000200)
            self.assertIs(process_module._run_taskkill(321, force=False), result)

        self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    @unittest.skipUnless(sys.platform == "win32", "Windows console handles only")
    def test_windows_detached_child_has_no_console_handle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            marker = Path(temporary_directory) / "console-handle"
            script = (
                "import ctypes,pathlib,sys;"
                "handle=ctypes.windll.kernel32.GetConsoleWindow();"
                "pathlib.Path(sys.argv[1]).write_text("
                "str(int(handle or 0)),encoding='utf-8')"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(marker)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=windows_detached_flags(),
            )
            try:
                self.assertEqual(process.wait(timeout=10), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

            self.assertEqual(marker.read_text(encoding="utf-8"), "0")

    @unittest.skipUnless(sys.platform == "win32", "Windows handles only")
    def test_windows_process_anchor_stress_does_not_leak_handles(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessHandleCount.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.GetProcessHandleCount.restype = wintypes.BOOL

        def handle_count() -> int:
            count = wintypes.DWORD()
            self.assertTrue(
                kernel32.GetProcessHandleCount(
                    kernel32.GetCurrentProcess(),
                    ctypes.byref(count),
                )
            )
            return count.value

        with process_module._windows_process_anchor(os.getpid()) as anchor:
            self.assertFalse(anchor.is_signaled())
        before = handle_count()
        for _ in range(200):
            with process_module._windows_process_anchor(os.getpid()) as anchor:
                self.assertFalse(anchor.is_signaled())
        after = handle_count()

        self.assertLessEqual(after, before + 1)

    @unittest.skipUnless(
        sys.platform == "linux" and callable(getattr(os, "pidfd_open", None)),
        "Linux pidfds only",
    )
    def test_linux_process_anchor_stress_does_not_leak_pidfds(self) -> None:
        def file_descriptor_count() -> int:
            return len(tuple(Path("/proc/self/fd").iterdir()))

        with process_module._linux_process_anchor(os.getpid()) as anchor:
            self.assertIsInstance(anchor, process_module._LinuxPidfdAnchor)
            self.assertFalse(anchor.is_signaled())
        before = file_descriptor_count()
        for _ in range(200):
            with process_module._linux_process_anchor(os.getpid()) as anchor:
                self.assertIsInstance(anchor, process_module._LinuxPidfdAnchor)
                self.assertFalse(anchor.is_signaled())
        after = file_descriptor_count()

        self.assertLessEqual(after, before + 1)

    def test_windows_anchor_open_errors_have_stable_codes(self) -> None:
        import ctypes

        class FakeFunction:
            argtypes: object = None
            restype: object = None

            def __call__(self, *arguments: object) -> int:
                return 0

        class FakeKernel32:
            OpenProcess = FakeFunction()

        for winerror, expected_code in (
            (87, "process_not_found"),
            (1168, "process_not_found"),
            (5, "invalid_state"),
            (31, "invalid_state"),
        ):
            with self.subTest(winerror=winerror), patch.object(
                ctypes,
                "WinDLL",
                return_value=FakeKernel32(),
                create=True,
            ), patch.object(
                ctypes,
                "get_last_error",
                return_value=winerror,
                create=True,
            ):
                with self.assertRaises(WorkerError) as raised:
                    process_module._windows_open_process_handle(321)

            self.assertEqual(raised.exception.code, expected_code)
            if expected_code == "invalid_state":
                self.assertEqual(raised.exception.details["winerror"], winerror)

    def test_linux_pidfd_poll_supports_file_descriptors_above_fd_setsize(
        self,
    ) -> None:
        high_file_descriptor = 4096

        class FakePoll:
            def __init__(inner_self) -> None:
                inner_self.registered: list[tuple[int, int]] = []

            def register(inner_self, file_descriptor: int, events: int) -> None:
                inner_self.registered.append((file_descriptor, events))

            def poll(inner_self, timeout: int) -> list[tuple[int, int]]:
                self.assertEqual(timeout, 0)
                return []

        poller = FakePoll()
        with patch.object(
            process_module.select,
            "poll",
            return_value=poller,
            create=True,
        ):
            self.assertFalse(
                process_module._pidfd_is_signaled(high_file_descriptor)
            )

        self.assertEqual(poller.registered[0][0], high_file_descriptor)

    def test_linux_pidfd_signal_has_stable_fail_closed_errors(self) -> None:
        anchor = process_module._LinuxPidfdAnchor(321, 99)
        cases = (
            (ProcessLookupError(), None),
            (OSError(errno.ENOSYS, "not supported"), "invalid_state"),
            (PermissionError(errno.EPERM, "denied"), "invalid_state"),
        )

        for error, expected_code in cases:
            with self.subTest(error=error), patch.object(
                process_module.signal,
                "pidfd_send_signal",
                side_effect=error,
                create=True,
            ):
                if expected_code is None:
                    self.assertFalse(
                        process_module._send_linux_pidfd_signal(
                            anchor,
                            signal.SIGTERM,
                        )
                    )
                else:
                    with self.assertRaises(WorkerError) as raised:
                        process_module._send_linux_pidfd_signal(
                            anchor,
                            signal.SIGTERM,
                        )
                    self.assertEqual(raised.exception.code, expected_code)

        with patch.object(
            process_module.signal,
            "pidfd_send_signal",
            None,
            create=True,
        ), self.assertRaises(WorkerError) as raised:
            process_module._send_linux_pidfd_signal(anchor, signal.SIGTERM)
        self.assertEqual(raised.exception.code, "invalid_state")


class PlatformParsingTests(unittest.TestCase):
    def test_linux_stat_parser_handles_spaces_and_parentheses_in_comm(self) -> None:
        stat_line = (
            "321 (worker name (nested) value) S "
            + " ".join(str(field) for field in range(4, 22))
            + " 987654 0 0"
        )

        state, start_marker = process_module._parse_linux_stat(stat_line)

        self.assertEqual(state, "S")
        self.assertEqual(start_marker, "987654")

    def test_linux_zombie_state_is_treated_as_gone(self) -> None:
        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "_read_linux_stat",
            return_value=("Z", "987654"),
        ):
            self.assertTrue(process_module._process_is_gone(321))

    def test_linux_group_scan_ignores_zombies_but_finds_live_members(self) -> None:
        def stat_line(pid: int, state: str, process_group: int) -> str:
            fields = [state, "1", str(process_group), *(["0"] * 16), "987654"]
            return f"{pid} (worker ({pid})) " + " ".join(fields)

        with tempfile.TemporaryDirectory() as directory:
            proc_root = Path(directory)
            zombie_directory = proc_root / "101"
            zombie_directory.mkdir()
            (zombie_directory / "stat").write_text(
                stat_line(101, "Z", 4321),
                encoding="utf-8",
            )
            unrelated_directory = proc_root / "102"
            unrelated_directory.mkdir()
            (unrelated_directory / "stat").write_text(
                stat_line(102, "S", 9999),
                encoding="utf-8",
            )

            self.assertFalse(
                process_module._linux_process_group_has_live_members(
                    4321,
                    proc_root,
                )
            )

            live_directory = proc_root / "103"
            live_directory.mkdir()
            (live_directory / "stat").write_text(
                stat_line(103, "R", 4321),
                encoding="utf-8",
            )
            self.assertTrue(
                process_module._linux_process_group_has_live_members(
                    4321,
                    proc_root,
                )
            )

            members = process_module._linux_process_group_members(
                4321,
                proc_root,
            )

        self.assertEqual(
            [
                (member.pid, member.process_group, member.start_marker)
                for member in members
            ],
            [(103, 4321, "987654")],
        )

    def test_macos_ps_parser_separates_start_marker_and_command(self) -> None:
        output = (
            "Fri Jul 17 12:34:56 2026 "
            "/usr/bin/python3 -c pass ownership-nonce\n"
        )

        identity = process_module._parse_macos_identity(321, output)

        self.assertEqual(identity.pid, 321)
        self.assertEqual(identity.start_marker, "Fri Jul 17 12:34:56 2026")
        self.assertEqual(
            identity.command_line,
            "/usr/bin/python3 -c pass ownership-nonce",
        )

    def test_macos_group_scan_ignores_zombies_but_finds_live_members(self) -> None:
        zombie_only = "101 4321 Z+\n102 9999 S\n"
        with_live_member = zombie_only + "103 4321 S+\n"

        self.assertFalse(
            process_module._parse_macos_process_group_has_live_members(
                4321,
                zombie_only,
            )
        )
        self.assertTrue(
            process_module._parse_macos_process_group_has_live_members(
                4321,
                with_live_member,
            )
        )

    def test_windows_json_parser_requires_identity_fields(self) -> None:
        payload = json.dumps(
            {
                "CreationDate": "/Date(1784296771084)/",
                "CommandLine": '"python.exe" worker.py ownership-nonce',
            }
        )

        identity = process_module._parse_windows_identity(321, payload)

        self.assertEqual(identity.pid, 321)
        self.assertEqual(identity.start_marker, "/Date(1784296771084)/")
        self.assertIn("ownership-nonce", identity.command_line)

    @unittest.skipUnless(sys.platform == "win32", "Windows tokenizer only")
    def test_windows_tokenizer_preserves_quoted_argument_boundaries(self) -> None:
        tokens = process_module._windows_command_tokens(
            '"C:\\Program Files\\Python\\python.exe" '
            '-c "print(\'quoted-nonce\')" "quoted-nonce"'
        )

        self.assertEqual(tokens[-1], "quoted-nonce")
        self.assertNotIn("quoted-nonce", tokens[:-1])


class SystemCommandTests(unittest.TestCase):
    def test_system_command_timeouts_are_invalid_state(self) -> None:
        operations = (
            process_module._read_windows_identity,
            process_module._read_macos_identity,
            process_module._macos_process_is_gone,
            process_module._macos_process_group_has_live_members,
            lambda pid: process_module._run_taskkill(pid, force=False),
        )

        for operation in operations:
            timeout = subprocess.TimeoutExpired(["system-command"], 10.0)
            with self.subTest(operation=operation), patch.object(
                process_module.subprocess,
                "run",
                side_effect=timeout,
            ):
                with self.assertRaises(WorkerError) as raised:
                    operation(321)

            self.assertEqual(raised.exception.code, "invalid_state")
            self.assertEqual(raised.exception.details["timeout_seconds"], 10.0)

    def test_macos_ps_uses_wide_output_and_finite_timeout(self) -> None:
        long_nonce = "n" * 4096
        identity_result = subprocess.CompletedProcess(
            [],
            0,
            f"Fri Jul 17 12:34:56 2026 python worker.py {long_nonce}\n",
            "",
        )
        status_result = subprocess.CompletedProcess([], 0, "S\n", "")
        with patch.object(
            process_module.subprocess,
            "run",
            side_effect=(identity_result, status_result),
        ) as run:
            identity = process_module._read_macos_identity(321)
            self.assertFalse(process_module._macos_process_is_gone(321))

        self.assertTrue(identity.command_line.endswith(long_nonce))
        self.assertEqual(
            run.call_args_list[0].args[0][:4],
            ["ps", "-ww", "-p", "321"],
        )
        self.assertEqual(
            run.call_args_list[1].args[0][:4],
            ["ps", "-ww", "-p", "321"],
        )
        for invocation in run.call_args_list:
            self.assertEqual(invocation.kwargs["timeout"], 10.0)

    def test_macos_group_scan_uses_all_processes_and_finite_timeout(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            0,
            "101 4321 Z+\n102 4321 S+\n",
            "",
        )
        with patch.object(
            process_module.subprocess,
            "run",
            return_value=result,
        ) as run:
            self.assertTrue(
                process_module._macos_process_group_has_live_members(4321)
            )

        self.assertEqual(
            run.call_args.args[0],
            [
                "ps",
                "-ww",
                "-A",
                "-o",
                "pid=",
                "-o",
                "pgid=",
                "-o",
                "stat=",
            ],
        )
        self.assertEqual(run.call_args.kwargs["timeout"], 10.0)

    def test_windows_query_and_taskkill_use_finite_timeout(self) -> None:
        identity_result = subprocess.CompletedProcess(
            [],
            0,
            json.dumps(
                {
                    "CreationDate": "/Date(1784296771084)/",
                    "CommandLine": "python worker.py ownership-nonce",
                }
            ),
            "",
        )
        taskkill_result = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(
            process_module.subprocess,
            "run",
            side_effect=(identity_result, taskkill_result),
        ) as run:
            process_module._read_windows_identity(321)
            process_module._run_taskkill(321, force=False)

        for invocation in run.call_args_list:
            self.assertEqual(invocation.kwargs["timeout"], 10.0)
            expected_flags = (
                subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            self.assertEqual(
                invocation.kwargs.get("creationflags"),
                expected_flags,
            )


class TerminationSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pid = 4321
        self.nonce = "ownership-nonce"
        self.identity = ProcessIdentity(
            self.pid,
            "start-marker",
            f"python worker.py {self.nonce}",
        )

    def test_wrong_start_marker_refuses_without_signalling(self) -> None:
        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(process_module, "_get_process_group") as getpgid, patch.object(
            process_module.os, "kill"
        ) as kill:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    "wrong-start-marker",
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "process_identity_mismatch")
        getpgid.assert_not_called()
        kill.assert_not_called()

    def test_process_group_mismatch_refuses_without_signalling(self) -> None:
        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid + 1,
        ), patch.object(process_module.os, "kill") as kill:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    self.identity.start_marker,
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "process_identity_mismatch")
        kill.assert_not_called()

    def test_posix_terminates_only_the_verified_negative_process_group(self) -> None:
        with patch.object(process_module.sys, "platform", "darwin"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_wait_until_process_group_gone",
            return_value=True,
        ), patch.object(process_module.os, "kill") as kill:
            terminate_owned_tree(
                self.pid,
                self.identity.start_marker,
                self.nonce,
                0,
            )

        kill.assert_called_once_with(-self.pid, signal.SIGTERM)

    def test_posix_revalidates_before_sigkill(self) -> None:
        with patch.object(process_module.sys, "platform", "darwin"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ) as read_identity, patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_wait_until_process_group_gone",
            side_effect=(False, True),
        ), patch.object(
            process_module,
            "_process_is_gone",
            return_value=False,
        ), patch.object(process_module.os, "kill") as kill:
            terminate_owned_tree(
                self.pid,
                self.identity.start_marker,
                self.nonce,
                0,
            )

        self.assertEqual(read_identity.call_count, 6)
        self.assertEqual(
            kill.call_args_list,
            [
                call(-self.pid, signal.SIGTERM),
                call(-self.pid, process_module._SIGKILL),
            ],
        )

    def test_posix_escalates_when_leader_exits_but_group_remains(self) -> None:
        with patch.object(process_module.sys, "platform", "darwin"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ) as read_identity, patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "wait_until_gone",
            return_value=True,
        ), patch.object(
            process_module,
            "_wait_until_process_group_gone",
            side_effect=(False, True),
            create=True,
        ), patch.object(
            process_module,
            "_process_is_gone",
            return_value=True,
        ), patch.object(process_module.os, "kill") as kill:
            terminate_owned_tree(
                self.pid,
                self.identity.start_marker,
                self.nonce,
                0,
            )

        self.assertEqual(read_identity.call_count, 4)
        self.assertEqual(
            kill.call_args_list,
            [
                call(-self.pid, signal.SIGTERM),
                call(-self.pid, process_module._SIGKILL),
            ],
        )

    def test_posix_group_probe_ignores_zombie_only_group(self) -> None:
        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module.os,
            "kill",
        ) as kill, patch.object(
            process_module,
            "_linux_process_group_has_live_members",
            return_value=False,
            create=True,
        ) as has_live_members:
            self.assertTrue(process_module._process_group_is_gone(self.pid))

        self.assertEqual(kill.call_args_list, [call(-self.pid, 0)] * 3)
        self.assertEqual(
            has_live_members.call_args_list,
            [call(self.pid), call(self.pid)],
        )

    def test_posix_group_probe_waits_for_live_group_member(self) -> None:
        with patch.object(process_module.sys, "platform", "darwin"), patch.object(
            process_module.os,
            "kill",
        ) as kill, patch.object(
            process_module,
            "_macos_process_group_has_live_members",
            return_value=True,
            create=True,
        ) as has_live_members:
            self.assertFalse(process_module._process_group_is_gone(self.pid))

        kill.assert_called_once_with(-self.pid, 0)
        has_live_members.assert_called_once_with(self.pid)

    def test_linux_transient_empty_group_still_escalates_for_new_live_member(
        self,
    ) -> None:
        leader = process_module._LinuxProcessGroupMember(
            self.pid,
            self.pid,
            self.identity.start_marker,
        )
        descendant = process_module._LinuxProcessGroupMember(
            self.pid + 1,
            self.pid,
            "descendant-start",
        )
        snapshots = iter(
            (
                (leader,),
                (),
                (leader, descendant),
                (leader, descendant),
                (leader, descendant),
                (leader, descendant),
                (descendant,),
                (),
                (),
            )
        )
        signaled_file_descriptors: set[int] = set()
        pidfd_signals: list[tuple[int, int]] = []

        def members(
            process_group: int,
            proc_root: Path = Path("/proc"),
        ) -> tuple[object, ...]:
            self.assertEqual(process_group, self.pid)
            return next(snapshots)

        def send_pidfd_signal(
            file_descriptor: int,
            signal_number: int,
            siginfo: object,
            flags: int,
        ) -> None:
            self.assertIsNone(siginfo)
            self.assertEqual(flags, 0)
            pidfd_signals.append((file_descriptor, signal_number))
            if file_descriptor == 100 or signal_number == process_module._SIGKILL:
                signaled_file_descriptors.add(file_descriptor)

        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_linux_process_group_members",
            side_effect=members,
        ), patch.object(
            process_module,
            "_read_linux_process_group_member",
            return_value=descendant,
        ), patch.object(
            process_module.os,
            "pidfd_open",
            side_effect=(100, 101),
            create=True,
        ), patch.object(
            process_module,
            "_pidfd_is_signaled",
            side_effect=lambda fd: fd in signaled_file_descriptors,
        ), patch.object(
            process_module.signal,
            "pidfd_send_signal",
            side_effect=send_pidfd_signal,
            create=True,
        ), patch.object(process_module.os, "kill") as kill, patch.object(
            process_module.os,
            "close",
        ):
            terminate_owned_tree(
                self.pid,
                self.identity.start_marker,
                self.nonce,
                0,
            )

        self.assertEqual(
            pidfd_signals,
            [
                (101, signal.SIGTERM),
                (100, signal.SIGTERM),
                (101, process_module._SIGKILL),
            ],
        )
        self.assertFalse(
            any(invocation.args[1] != 0 for invocation in kill.call_args_list)
        )

    def test_macos_transient_empty_snapshot_rechecks_before_gone(self) -> None:
        zombie_only = subprocess.CompletedProcess(
            [],
            0,
            f"101 {self.pid} Z+\n",
            "",
        )
        new_live_member = subprocess.CompletedProcess(
            [],
            0,
            f"101 {self.pid} Z+\n102 {self.pid} S+\n",
            "",
        )
        with patch.object(process_module.sys, "platform", "darwin"), patch.object(
            process_module.os,
            "kill",
        ) as kill, patch.object(
            process_module.subprocess,
            "run",
            side_effect=(zombie_only, new_live_member),
        ) as run:
            self.assertFalse(process_module._process_group_is_gone(self.pid))

        self.assertEqual(kill.call_args_list, [call(-self.pid, 0)] * 2)
        self.assertEqual(run.call_count, 2)
        for invocation in run.call_args_list:
            self.assertEqual(invocation.kwargs["timeout"], 10.0)

    def test_windows_uses_exact_pid_tree_commands_and_revalidates_force(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        windows_identity = ProcessIdentity(
            self.pid,
            self.identity.start_marker,
            f"python\0worker.py\0{self.nonce}",
        )
        with patch.object(process_module.sys, "platform", "win32"), patch.object(
            process_module,
            "_windows_process_anchor",
            return_value=process_module._UnstableProcessAnchorContext(self.pid),
        ), patch.object(
            process_module,
            "read_process_identity",
            return_value=windows_identity,
        ) as read_identity, patch.object(
            process_module,
            "wait_until_gone",
            side_effect=(False, True),
        ), patch.object(
            process_module.subprocess,
            "run",
            side_effect=(completed, completed),
        ) as run, patch.object(
            process_module,
            "_windows_command_tokens",
            side_effect=AssertionError("platform tokenizer must not be used"),
        ) as windows_command_tokens:
            terminate_owned_tree(
                self.pid,
                self.identity.start_marker,
                self.nonce,
                0,
            )

        self.assertEqual(read_identity.call_count, 4)
        windows_command_tokens.assert_not_called()
        self.assertEqual(
            [invocation.args[0] for invocation in run.call_args_list],
            [
                ["taskkill", "/PID", str(self.pid), "/T"],
                ["taskkill", "/PID", str(self.pid), "/T", "/F"],
            ],
        )
        for invocation in run.call_args_list:
            self.assertNotIn("shell", invocation.kwargs)

    def test_posix_identity_change_between_checks_refuses_before_signal(self) -> None:
        changed_identity = ProcessIdentity(
            self.pid,
            "reused-start-marker",
            f"python worker.py {self.nonce}",
        )
        with patch.object(process_module.sys, "platform", "darwin"), patch.object(
            process_module,
            "read_process_identity",
            side_effect=(self.identity, changed_identity),
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_wait_until_process_group_gone",
            return_value=True,
            create=True,
        ), patch.object(
            process_module,
            "wait_until_gone",
            return_value=True,
        ), patch.object(process_module.os, "kill") as kill:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    self.identity.start_marker,
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "process_identity_mismatch")
        kill.assert_not_called()

    def test_windows_identity_change_closes_anchor_before_taskkill(self) -> None:
        events: list[str] = []
        changed_identity = ProcessIdentity(
            self.pid,
            "reused-start-marker",
            f"python\0worker.py\0{self.nonce}",
        )

        class FakeAnchor:
            def is_signaled(self) -> bool:
                return False

        class FakeAnchorContext:
            def __enter__(inner_self) -> FakeAnchor:
                events.append("open")
                return FakeAnchor()

            def __exit__(
                inner_self,
                exc_type: object,
                exc: object,
                traceback: object,
            ) -> bool:
                events.append("close")
                return False

        def anchor(pid: int) -> FakeAnchorContext:
            self.assertEqual(pid, self.pid)
            return FakeAnchorContext()

        with patch.object(process_module.sys, "platform", "win32"), patch.object(
            process_module,
            "_windows_process_anchor",
            side_effect=anchor,
            create=True,
        ), patch.object(
            process_module,
            "read_process_identity",
            side_effect=(self.identity, changed_identity),
        ), patch.object(
            process_module,
            "wait_until_gone",
            return_value=True,
        ), patch.object(process_module.subprocess, "run") as run:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    self.identity.start_marker,
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "process_identity_mismatch")
        self.assertEqual(events, ["open", "close"])
        run.assert_not_called()

    def test_linux_pidfd_anchor_opens_before_identity_and_closes(self) -> None:
        events: list[str] = []
        mismatched_identity = ProcessIdentity(
            self.pid,
            "wrong-start-marker",
            f"python\0worker.py\0{self.nonce}",
        )

        def open_pidfd(pid: int, flags: int = 0) -> int:
            self.assertEqual((pid, flags), (self.pid, 0))
            events.append("open")
            return 99

        def read_identity(pid: int) -> ProcessIdentity:
            self.assertEqual(pid, self.pid)
            events.append("identity")
            return mismatched_identity

        def close_pidfd(file_descriptor: int) -> None:
            self.assertEqual(file_descriptor, 99)
            events.append("close")

        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module.os,
            "pidfd_open",
            side_effect=open_pidfd,
            create=True,
        ), patch.object(
            process_module.os,
            "close",
            side_effect=close_pidfd,
        ), patch.object(
            process_module,
            "_pidfd_is_signaled",
            return_value=False,
            create=True,
        ), patch.object(
            process_module,
            "read_process_identity",
            side_effect=read_identity,
        ), patch.object(process_module.os, "kill") as kill:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    self.identity.start_marker,
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "process_identity_mismatch")
        self.assertEqual(events, ["open", "identity", "close"])
        kill.assert_not_called()

    def test_linux_signals_anchored_descendant_before_leader_and_closes_pidfds(
        self,
    ) -> None:
        leader = process_module._LinuxProcessGroupMember(
            self.pid,
            self.pid,
            self.identity.start_marker,
        )
        descendant = process_module._LinuxProcessGroupMember(
            self.pid + 1,
            self.pid,
            "descendant-start",
        )
        phase = "initial"
        signaled_file_descriptors: set[int] = set()
        pidfd_signals: list[tuple[int, int]] = []

        def members(
            process_group: int,
            proc_root: Path = Path("/proc"),
        ) -> tuple[object, ...]:
            self.assertEqual(process_group, self.pid)
            if phase == "initial":
                return (leader, descendant)
            if phase == "term":
                return (descendant,)
            return ()

        def pidfd_is_signaled(file_descriptor: int) -> bool:
            return file_descriptor in signaled_file_descriptors

        def send_pidfd_signal(
            file_descriptor: int,
            signal_number: int,
            siginfo: object,
            flags: int,
        ) -> None:
            nonlocal phase
            self.assertIsNone(siginfo)
            self.assertEqual(flags, 0)
            pidfd_signals.append((file_descriptor, signal_number))
            if signal_number == signal.SIGTERM and file_descriptor == 100:
                phase = "term"
                signaled_file_descriptors.add(file_descriptor)
            elif signal_number == process_module._SIGKILL:
                phase = "kill"
                signaled_file_descriptors.add(file_descriptor)

        def numeric_signal(pid: int, signal_number: int) -> None:
            nonlocal phase
            if signal_number == signal.SIGTERM:
                phase = "term"
                signaled_file_descriptors.add(100)
            elif signal_number == process_module._SIGKILL:
                phase = "kill"
                signaled_file_descriptors.add(101)

        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_linux_process_group_members",
            side_effect=members,
        ), patch.object(
            process_module,
            "_read_linux_process_group_member",
            return_value=descendant,
            create=True,
        ), patch.object(
            process_module.os,
            "pidfd_open",
            side_effect=(100, 101),
            create=True,
        ), patch.object(
            process_module,
            "_pidfd_is_signaled",
            side_effect=pidfd_is_signaled,
        ), patch.object(
            process_module.signal,
            "pidfd_send_signal",
            side_effect=send_pidfd_signal,
            create=True,
        ), patch.object(
            process_module.os,
            "kill",
            side_effect=numeric_signal,
        ) as kill, patch.object(process_module.os, "close") as close:
            terminate_owned_tree(
                self.pid,
                self.identity.start_marker,
                self.nonce,
                0,
            )

        self.assertEqual(
            pidfd_signals,
            [
                (101, signal.SIGTERM),
                (100, signal.SIGTERM),
                (101, process_module._SIGKILL),
            ],
        )
        self.assertEqual(
            [
                invocation
                for invocation in kill.call_args_list
                if invocation.args[1] != 0
            ],
            [],
        )
        self.assertCountEqual(
            [invocation.args[0] for invocation in close.call_args_list],
            [100, 101],
        )

    def test_linux_refuses_reused_group_after_leader_exit_without_signalling(
        self,
    ) -> None:
        leader = process_module._LinuxProcessGroupMember(
            self.pid,
            self.pid,
            self.identity.start_marker,
        )
        reused = process_module._LinuxProcessGroupMember(
            self.pid,
            self.pid,
            "reused-start-marker",
        )
        phase = "initial"
        pidfd_signals: list[tuple[int, int]] = []

        def members(
            process_group: int,
            proc_root: Path = Path("/proc"),
        ) -> tuple[object, ...]:
            self.assertEqual(process_group, self.pid)
            if phase == "initial":
                return (leader,)
            if phase == "term":
                return (reused,)
            return ()

        def send_pidfd_signal(
            file_descriptor: int,
            signal_number: int,
            siginfo: object,
            flags: int,
        ) -> None:
            nonlocal phase
            self.assertEqual(file_descriptor, 100)
            self.assertIsNone(siginfo)
            self.assertEqual(flags, 0)
            pidfd_signals.append((file_descriptor, signal_number))
            phase = "term"
            raise ProcessLookupError(errno.ESRCH, "leader exited")

        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_linux_process_group_members",
            side_effect=members,
        ), patch.object(
            process_module.os,
            "pidfd_open",
            return_value=100,
            create=True,
        ), patch.object(
            process_module,
            "_pidfd_is_signaled",
            return_value=False,
        ), patch.object(
            process_module.signal,
            "pidfd_send_signal",
            side_effect=send_pidfd_signal,
            create=True,
        ), patch.object(process_module.os, "kill") as kill, patch.object(
            process_module.os,
            "close",
        ) as close:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    self.identity.start_marker,
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertIn("safely", str(raised.exception).lower())
        self.assertEqual(pidfd_signals, [(100, signal.SIGTERM)])
        self.assertEqual(
            [
                invocation
                for invocation in kill.call_args_list
                if invocation.args[1] != 0
            ],
            [],
        )
        close.assert_called_once_with(100)

    def test_linux_without_pidfd_support_fails_before_numeric_signal(self) -> None:
        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module.os,
            "pidfd_open",
            None,
            create=True,
        ), patch.object(
            process_module,
            "_wait_until_process_group_gone",
            return_value=True,
        ), patch.object(process_module.os, "kill") as kill:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    self.identity.start_marker,
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertEqual(
            [
                invocation
                for invocation in kill.call_args_list
                if invocation.args[1] != 0
            ],
            [],
        )

    def test_linux_descendant_pidfd_enosys_fails_closed(self) -> None:
        leader = process_module._LinuxProcessGroupMember(
            self.pid,
            self.pid,
            self.identity.start_marker,
        )
        descendant = process_module._LinuxProcessGroupMember(
            self.pid + 1,
            self.pid,
            "descendant-start",
        )
        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_linux_process_group_members",
            return_value=(leader, descendant),
        ), patch.object(
            process_module.os,
            "pidfd_open",
            side_effect=(100, OSError(errno.ENOSYS, "not supported")),
            create=True,
        ), patch.object(
            process_module,
            "_pidfd_is_signaled",
            return_value=False,
        ), patch.object(
            process_module.signal,
            "pidfd_send_signal",
            create=True,
        ) as sender, patch.object(process_module.os, "kill") as kill, patch.object(
            process_module.os,
            "close",
        ) as close:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    self.identity.start_marker,
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "invalid_state")
        sender.assert_not_called()
        self.assertFalse(
            any(invocation.args[1] != 0 for invocation in kill.call_args_list)
        )
        close.assert_called_once_with(100)

    def test_linux_signal_error_closes_all_anchored_pidfds(self) -> None:
        leader = process_module._LinuxProcessGroupMember(
            self.pid,
            self.pid,
            self.identity.start_marker,
        )
        descendant = process_module._LinuxProcessGroupMember(
            self.pid + 1,
            self.pid,
            "descendant-start",
        )
        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_linux_process_group_members",
            return_value=(leader, descendant),
        ), patch.object(
            process_module,
            "_read_linux_process_group_member",
            return_value=descendant,
        ), patch.object(
            process_module.os,
            "pidfd_open",
            side_effect=(100, 101),
            create=True,
        ), patch.object(
            process_module,
            "_pidfd_is_signaled",
            return_value=False,
        ), patch.object(
            process_module.signal,
            "pidfd_send_signal",
            side_effect=PermissionError(errno.EPERM, "denied"),
            create=True,
        ), patch.object(process_module.os, "kill") as kill, patch.object(
            process_module.os,
            "close",
        ) as close:
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    self.pid,
                    self.identity.start_marker,
                    self.nonce,
                    0,
                )

        self.assertEqual(raised.exception.code, "invalid_state")
        self.assertFalse(
            any(invocation.args[1] != 0 for invocation in kill.call_args_list)
        )
        self.assertCountEqual(
            [invocation.args[0] for invocation in close.call_args_list],
            [100, 101],
        )

    def test_linux_pidfd_esrch_is_treated_as_member_exit(self) -> None:
        leader = process_module._LinuxProcessGroupMember(
            self.pid,
            self.pid,
            self.identity.start_marker,
        )
        descendant = process_module._LinuxProcessGroupMember(
            self.pid + 1,
            self.pid,
            "descendant-start",
        )
        descendant_exited = False
        leader_exited = False
        pidfd_signals: list[tuple[int, int]] = []

        def members(
            process_group: int,
            proc_root: Path = Path("/proc"),
        ) -> tuple[object, ...]:
            self.assertEqual(process_group, self.pid)
            if not descendant_exited:
                return (leader, descendant)
            if not leader_exited:
                return (leader,)
            return ()

        def send_pidfd_signal(
            file_descriptor: int,
            signal_number: int,
            siginfo: object,
            flags: int,
        ) -> None:
            nonlocal descendant_exited, leader_exited
            self.assertIsNone(siginfo)
            self.assertEqual(flags, 0)
            pidfd_signals.append((file_descriptor, signal_number))
            if file_descriptor == 101:
                descendant_exited = True
                raise ProcessLookupError(errno.ESRCH, "exited")
            leader_exited = True

        with patch.object(process_module.sys, "platform", "linux"), patch.object(
            process_module,
            "read_process_identity",
            return_value=self.identity,
        ), patch.object(
            process_module,
            "_get_process_group",
            return_value=self.pid,
        ), patch.object(
            process_module,
            "_linux_process_group_members",
            side_effect=members,
        ), patch.object(
            process_module,
            "_read_linux_process_group_member",
            return_value=descendant,
        ), patch.object(
            process_module.os,
            "pidfd_open",
            side_effect=(100, 101),
            create=True,
        ), patch.object(
            process_module,
            "_pidfd_is_signaled",
            side_effect=lambda fd: fd == 100 and leader_exited,
        ), patch.object(
            process_module.signal,
            "pidfd_send_signal",
            side_effect=send_pidfd_signal,
            create=True,
        ), patch.object(process_module.os, "kill") as kill, patch.object(
            process_module.os,
            "close",
        ):
            terminate_owned_tree(
                self.pid,
                self.identity.start_marker,
                self.nonce,
                0,
            )

        self.assertEqual(
            pidfd_signals,
            [(101, signal.SIGTERM), (100, signal.SIGTERM)],
        )
        self.assertFalse(
            any(invocation.args[1] != 0 for invocation in kill.call_args_list)
        )


class DisposableChildTests(unittest.TestCase):
    def _start_child(
        self,
        nonce: str,
        *,
        isolated: bool = True,
    ) -> subprocess.Popen[bytes]:
        arguments = [
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            nonce,
        ]
        options: dict[str, object] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            options["creationflags"] = windows_detached_flags()
        elif isolated:
            options["start_new_session"] = True
        return subprocess.Popen(arguments, **options)  # type: ignore[arg-type]

    def _start_term_resistant_tree(
        self,
        nonce: str,
        descendant_pid_path: Path,
    ) -> subprocess.Popen[bytes]:
        descendant_code = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "time.sleep(60)"
        )
        leader_code = (
            "import pathlib,subprocess,sys,time;"
            "child=subprocess.Popen("
            "[sys.executable,'-c',sys.argv[1],sys.argv[2]],"
            "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL);"
            "pathlib.Path(sys.argv[3]).write_text("
            "str(child.pid),encoding='utf-8');"
            "time.sleep(60)"
        )
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                leader_code,
                descendant_code,
                nonce,
                str(descendant_pid_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _wait_for_descendant_pid(self, path: Path) -> int:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                content = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                time.sleep(0.05)
                continue
            if content:
                return int(content)
        self.fail("descendant PID was not published")

    def _cleanup_descendant(
        self,
        pid: int,
        identity: ProcessIdentity,
        nonce: str,
    ) -> None:
        original_test_error = sys.exc_info()[0] is not None
        try:
            if wait_until_gone(pid, 0):
                return
            current = read_process_identity(pid)
            if not identity_matches(current, identity.start_marker, nonce):
                return
            os.kill(pid, process_module._SIGKILL)
            if not wait_until_gone(pid, 5):
                raise AssertionError(f"descendant did not exit: {pid}")
        except BaseException:
            if not original_test_error:
                raise

    def _terminate_child_directly(
        self,
        child: subprocess.Popen[bytes],
    ) -> None:
        if child.poll() is None:
            child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)

    def _cleanup_child(
        self,
        child: subprocess.Popen[bytes],
        identity: ProcessIdentity | None = None,
        nonce: str | None = None,
    ) -> None:
        original_test_error = sys.exc_info()[0] is not None
        cleanup_error: BaseException | None = None
        try:
            if child.poll() is not None:
                child.wait(timeout=5)
                return
            if identity is None or nonce is None:
                self._terminate_child_directly(child)
                return
            terminate_owned_tree(
                child.pid,
                identity.start_marker,
                nonce,
                grace_seconds=0.1,
            )
            child.wait(timeout=5)
        except BaseException as error:
            cleanup_error = error
            try:
                self._terminate_child_directly(child)
            except BaseException as fallback_error:
                if not original_test_error:
                    raise fallback_error from error
        if cleanup_error is not None and not original_test_error:
            raise cleanup_error

    def test_disposable_owned_child_is_terminated_without_residue_twice(self) -> None:
        for attempt in range(2):
            nonce = f"owned-child-{os.getpid()}-{attempt}"
            identity: ProcessIdentity | None = None
            child = self._start_child(nonce)
            try:
                identity = read_process_identity(child.pid)
                terminate_owned_tree(
                    child.pid,
                    identity.start_marker,
                    nonce,
                    grace_seconds=0.1,
                )
                self.assertTrue(wait_until_gone(child.pid, 5))
                child.wait(timeout=5)
            finally:
                self._cleanup_child(child, identity, nonce)

    def test_wrong_nonce_refuses_and_leaves_child_alive(self) -> None:
        nonce = f"owned-child-{os.getpid()}-mismatch"
        identity: ProcessIdentity | None = None
        child = self._start_child(nonce)
        try:
            identity = read_process_identity(child.pid)
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    child.pid,
                    identity.start_marker,
                    f"wrong-{nonce}",
                    grace_seconds=0.1,
                )

            self.assertEqual(
                raised.exception.code,
                "process_identity_mismatch",
            )
            self.assertIsNone(child.poll())
        finally:
            self._cleanup_child(child, identity, nonce)

    def test_wrong_start_marker_refuses_and_leaves_child_alive(self) -> None:
        nonce = f"owned-child-{os.getpid()}-wrong-start"
        identity: ProcessIdentity | None = None
        child = self._start_child(nonce)
        try:
            identity = read_process_identity(child.pid)
            with self.assertRaises(WorkerError) as raised:
                terminate_owned_tree(
                    child.pid,
                    f"{identity.start_marker}-wrong",
                    nonce,
                    grace_seconds=0.1,
                )

            self.assertEqual(
                raised.exception.code,
                "process_identity_mismatch",
            )
            self.assertIsNone(child.poll())
        finally:
            self._cleanup_child(child, identity, nonce)

    @unittest.skipIf(sys.platform == "win32", "POSIX process groups only")
    def test_non_leader_child_refuses_without_group_signal(self) -> None:
        nonce = f"owned-child-{os.getpid()}-shared-group"
        identity: ProcessIdentity | None = None
        child = self._start_child(nonce, isolated=False)
        try:
            identity = read_process_identity(child.pid)
            self.assertNotEqual(os.getpgid(child.pid), child.pid)
            with patch.object(process_module.os, "kill") as kill:
                with self.assertRaises(WorkerError) as raised:
                    terminate_owned_tree(
                        child.pid,
                        identity.start_marker,
                        nonce,
                        grace_seconds=0.1,
                    )

            self.assertEqual(
                raised.exception.code,
                "process_identity_mismatch",
            )
            self.assertIsNone(child.poll())
            kill.assert_not_called()
        finally:
            self._cleanup_child(child)

    @unittest.skipIf(sys.platform == "win32", "POSIX process groups only")
    def test_leader_exit_does_not_hide_term_resistant_descendant(self) -> None:
        nonce = f"owned-child-{os.getpid()}-term-resistant-tree"
        with tempfile.TemporaryDirectory() as temporary_directory:
            descendant_pid_path = Path(temporary_directory) / "descendant.pid"
            leader_identity: ProcessIdentity | None = None
            descendant_pid: int | None = None
            descendant_identity: ProcessIdentity | None = None
            leader = self._start_term_resistant_tree(nonce, descendant_pid_path)
            try:
                leader_identity = read_process_identity(leader.pid)
                descendant_pid = self._wait_for_descendant_pid(descendant_pid_path)
                descendant_identity = read_process_identity(descendant_pid)
                terminate_owned_tree(
                    leader.pid,
                    leader_identity.start_marker,
                    nonce,
                    grace_seconds=0.2,
                )

                leader.wait(timeout=5)
                self.assertTrue(wait_until_gone(descendant_pid, 5))
                self.assertTrue(
                    process_module._wait_until_process_group_gone(leader.pid, 5)
                )
            finally:
                self._cleanup_child(leader, leader_identity, nonce)
                if descendant_pid is not None and descendant_identity is not None:
                    self._cleanup_descendant(
                        descendant_pid,
                        descendant_identity,
                        nonce,
                    )

    def test_cleanup_falls_back_to_popen_after_owned_termination_error(self) -> None:
        nonce = f"owned-child-{os.getpid()}-cleanup-fallback"
        identity: ProcessIdentity | None = None
        cleanup_error = WorkerError(
            "invalid_state",
            "simulated cleanup failure",
            {},
        )
        child = self._start_child(nonce)
        try:
            identity = read_process_identity(child.pid)
            with patch(
                f"{__name__}.terminate_owned_tree",
                side_effect=cleanup_error,
            ), self.assertRaises(WorkerError) as raised:
                self._cleanup_child(child, identity, nonce)

            self.assertIs(raised.exception, cleanup_error)
            self.assertIsNotNone(child.poll())
        finally:
            self._terminate_child_directly(child)

    def test_cleanup_does_not_mask_original_test_error(self) -> None:
        nonce = f"owned-child-{os.getpid()}-cleanup-original"
        identity: ProcessIdentity | None = None
        cleanup_error = WorkerError(
            "invalid_state",
            "simulated cleanup failure",
            {},
        )
        child = self._start_child(nonce)
        try:
            identity = read_process_identity(child.pid)
            with self.assertRaisesRegex(RuntimeError, "original test failure"):
                try:
                    raise RuntimeError("original test failure")
                finally:
                    with patch(
                        f"{__name__}.terminate_owned_tree",
                        side_effect=cleanup_error,
                    ):
                        self._cleanup_child(child, identity, nonce)
            self.assertIsNotNone(child.poll())
        finally:
            self._terminate_child_directly(child)

    def test_identity_query_failure_after_launch_uses_popen_fallback(self) -> None:
        nonce = f"owned-child-{os.getpid()}-identity-query-failure"
        identity: ProcessIdentity | None = None
        query_error = WorkerError(
            "invalid_state",
            "simulated identity query failure",
            {},
        )
        direct_cleanup = self._terminate_child_directly
        child = self._start_child(nonce)
        try:
            with patch.object(
                self,
                "_terminate_child_directly",
                wraps=direct_cleanup,
            ) as fallback, self.assertRaises(WorkerError) as raised:
                try:
                    with patch(
                        f"{__name__}.read_process_identity",
                        side_effect=query_error,
                    ):
                        identity = read_process_identity(child.pid)
                finally:
                    self._cleanup_child(child, identity, nonce)

            self.assertIs(raised.exception, query_error)
            fallback.assert_called_once_with(child)
            self.assertIsNotNone(child.poll())
        finally:
            self._terminate_child_directly(child)


if __name__ == "__main__":
    unittest.main()
