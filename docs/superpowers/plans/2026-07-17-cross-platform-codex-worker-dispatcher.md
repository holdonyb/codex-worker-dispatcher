# Cross-Platform Codex Worker Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish an Apache-2.0, installable Python CLI and Codex Agent Skill that run observable, scoped, and safely reclaimable local Codex workers on Windows, macOS, and Linux.

**Architecture:** A Python controller persists each task as an atomic filesystem state machine, a detached supervisor owns one runner, and a small platform adapter verifies process identity before terminating a process tree or group. The CLI emits one JSON value per action; the bundled Agent Skill teaches a parent agent to assign bounded work and close every worker lifecycle.

**Tech Stack:** Python 3.10+, standard library, setuptools, unittest, Codex CLI, GitHub Actions, GitHub CLI

**Execution status:** Completed on 2026-07-18. The sanitized public repository
passed its six-job Windows, Ubuntu, and macOS matrix in
[GitHub Actions run 29630973235](https://github.com/holdonyb/codex-worker-dispatcher/actions/runs/29630973235)
and was released as
[v0.1.0](https://github.com/holdonyb/codex-worker-dispatcher/releases/tag/v0.1.0).

---

## File Map

| Path | Responsibility |
|---|---|
| `pyproject.toml` | Package metadata, console entry point, wheel data files |
| `src/codex_worker_dispatcher/errors.py` | Stable public error envelope |
| `src/codex_worker_dispatcher/routing.py` | Complexity, intent, allowed-path, sandbox, and model/reasoning resolution |
| `src/codex_worker_dispatcher/state.py` | Task IDs, state root, atomic manifests, task files, terminal states |
| `src/codex_worker_dispatcher/process.py` | Windows/Linux/macOS process identity and verified tree/group termination |
| `src/codex_worker_dispatcher/runner.py` | Fake engine or concrete `codex exec` invocation |
| `src/codex_worker_dispatcher/supervisor.py` | One worker's TTL, cancellation, exit, and terminal-state ownership |
| `src/codex_worker_dispatcher/lifecycle.py` | Route/start/status/wait/result/cancel/reap/list/reap-stale actions |
| `src/codex_worker_dispatcher/installer.py` | Atomic skill install, upgrade backup, and owned uninstall |
| `src/codex_worker_dispatcher/cli.py` | argparse contract, JSON stdout, exit codes |
| `skill/dispatching-codex-workers/` | Public Agent Skill and UI metadata |
| `tests/` | Unit, lifecycle, package, installer, public-audit, and skill tests |
| `.github/workflows/ci.yml` | Windows/Ubuntu/macOS test and packaging matrix |
| `README.md`, `README.zh-CN.md` | Complete public usage documentation |
| `SECURITY.md`, `CONTRIBUTING.md` | Safe disclosure and contribution workflow |
| `AGENTS.md`, `PROJECT_STATUS.md` | Durable repository execution context |

## Task 1: Establish the Package and Repository Contract

**Files:**

- Create: `tests/test_package.py`
- Create: `src/codex_worker_dispatcher/__init__.py`
- Create: `src/codex_worker_dispatcher/__main__.py`
- Create: `src/codex_worker_dispatcher/cli.py`
- Create: `src/codex_worker_dispatcher/errors.py`
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `AGENTS.md`
- Create: `PROJECT_STATUS.md`

- [ ] **Step 0: Create the project virtual environment**

```powershell
python -m venv .venv
./.venv/Scripts/python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ --upgrade pip setuptools wheel
```

Expected: `.venv` exists and the package tools install successfully from the
configured China-accessible mirror.

- [ ] **Step 1: Write the failing package contract test**

```python
# tests/test_package.py
from __future__ import annotations

import json
import subprocess
import sys
import unittest

import codex_worker_dispatcher
from codex_worker_dispatcher.errors import WorkerError


class PackageContractTests(unittest.TestCase):
    def test_version_is_public_preview(self) -> None:
        self.assertEqual(codex_worker_dispatcher.__version__, "0.1.0")

    def test_worker_error_has_stable_envelope(self) -> None:
        error = WorkerError("invalid_arguments", "bad input", {"field": "prompt"})
        self.assertEqual(
            error.to_dict(),
            {
                "ok": False,
                "error": {
                    "code": "invalid_arguments",
                    "message": "bad input",
                    "details": {"field": "prompt"},
                },
            },
        )

    def test_module_help_is_machine_invocable(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-m", "codex_worker_dispatcher", "--version"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout), {"version": "0.1.0"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test and verify the package is missing**

Run:

```powershell
./.venv/Scripts/python -m unittest tests.test_package -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'codex_worker_dispatcher'`.

- [ ] **Step 3: Create package metadata and the stable error type**

Use this `pyproject.toml` contract:

```toml
[build-system]
requires = ["setuptools>=77", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "codex-worker-dispatcher"
version = "0.1.0"
description = "Cross-platform, observable, and safely reclaimable local workers for OpenAI Codex CLI."
requires-python = ">=3.10"
license = "Apache-2.0"
authors = [{ name = "holdonyb" }]
keywords = ["codex", "codex-cli", "agent-skills", "ai-agents", "workers"]
classifiers = [
  "Development Status :: 3 - Alpha",
  "License :: OSI Approved :: Apache Software License",
  "Operating System :: MacOS",
  "Operating System :: Microsoft :: Windows",
  "Operating System :: POSIX :: Linux",
  "Programming Language :: Python :: 3",
  "Programming Language :: Python :: 3.10",
  "Programming Language :: Python :: 3.14",
]

[project.scripts]
codex-worker = "codex_worker_dispatcher.cli:main"

[project.urls]
Homepage = "https://github.com/holdonyb/codex-worker-dispatcher"
Issues = "https://github.com/holdonyb/codex-worker-dispatcher/issues"

[tool.setuptools]
package-dir = { "" = "src" }

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.data-files]
"share/codex-worker-dispatcher/skill/dispatching-codex-workers" = ["skill/dispatching-codex-workers/SKILL.md"]
"share/codex-worker-dispatcher/skill/dispatching-codex-workers/agents" = ["skill/dispatching-codex-workers/agents/openai.yaml"]
"share/codex-worker-dispatcher/skill/dispatching-codex-workers/references" = ["skill/dispatching-codex-workers/references/design.md"]

```

Implement `WorkerError` as an exception with `code`, `message`, `details`, and
`to_dict()`. Set `__version__ = "0.1.0"`. Make `__main__.py` import and call
`cli.main`. The initial CLI must support `--version` and print
`{"version":"0.1.0"}` followed by one newline.

- [ ] **Step 4: Create repository-local operating context**

`AGENTS.md` must state Python 3.10+, standard-library runtime, unittest,
machine-readable operational stdout with human-readable argparse help, no
`danger-full-access`, TDD, and the three-platform validation command.
`PROJECT_STATUS.md` must record the approved design path, current phase,
validation command, public repository target, and that the remote does not yet
exist. `.gitignore` must exclude `.venv/`, `dist/`, `build/`, `*.egg-info/`,
`__pycache__/`, `*.py[cod]`, `.coverage`, `.DS_Store`, `Thumbs.db`,
`worker-runs/`, `manifest.json`, `prompt.txt`, `events.jsonl`, `stderr.log`, and
`last-message.txt`.

- [ ] **Step 5: Run the focused test and the package metadata check**

Run:

```powershell
./.venv/Scripts/python -m pip install --no-build-isolation --no-deps -e .
./.venv/Scripts/python -m unittest tests.test_package -v
./.venv/Scripts/codex-worker --version
```

Expected: three tests pass and the final command prints `{"version":"0.1.0"}`.

- [ ] **Step 6: Commit the foundation**

```powershell
git add -- pyproject.toml .gitignore AGENTS.md PROJECT_STATUS.md src tests/test_package.py
git commit -m "build: establish dispatcher package"
```

## Task 2: Resolve Routes and Scoped Write Authorization

**Files:**

- Create: `tests/test_routing.py`
- Create: `src/codex_worker_dispatcher/routing.py`

- [ ] **Step 1: Write failing route tests**

Cover these exact cases in `tests/test_routing.py`:

```python
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codex_worker_dispatcher.errors import WorkerError
from codex_worker_dispatcher.routing import resolve_route


class RoutingTests(unittest.TestCase):
    def test_read_route_inherits_model_and_uses_low_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            route = resolve_route("inspect parser", Path(directory), "simple", "read", "auto", [], None, "auto")
        self.assertIsNone(route["model"])
        self.assertEqual(route["reasoning"], "low")
        self.assertEqual(route["sandbox"], "read-only")

    def test_scoped_write_route_normalizes_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            route = resolve_route("implement parser", root, "standard", "write", "auto", ["src/parser"], "gpt-5.6", "auto")
        self.assertEqual(route["model"], "gpt-5.6")
        self.assertEqual(route["reasoning"], "medium")
        self.assertEqual(route["sandbox"], "workspace-write")
        self.assertTrue(route["write_authorized"])
        self.assertEqual(route["allowed_paths"], [str((root / "src/parser").resolve())])

    def test_unscoped_write_stays_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            route = resolve_route("implement parser", Path(directory), "standard", "write", "auto", [], None, "auto")
        self.assertEqual(route["sandbox"], "read-only")
        self.assertFalse(route["write_authorized"])

    def test_explicit_workspace_write_requires_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(WorkerError, "requires write intent"):
                resolve_route("inspect", Path(directory), "simple", "read", "workspace-write", [], None, "auto")

    def test_allowed_path_cannot_escape_workdir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(WorkerError, "inside the work directory"):
                resolve_route("implement", Path(directory), "standard", "write", "auto", ["../outside"], None, "auto")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Verify the tests fail for the missing module**

Run `./.venv/Scripts/python -m unittest tests.test_routing -v`.

Expected: FAIL importing `codex_worker_dispatcher.routing`.

- [ ] **Step 3: Implement the minimal route resolver**

Define:

```python
REASONING_BY_COMPLEXITY = {
    "simple": "low",
    "standard": "medium",
    "complex": "high",
    "hard": "xhigh",
    "extreme": "xhigh",
}

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
```

Use the approved design rules exactly. Auto intent recognizes the English and
Chinese write verbs from the original skill, but resolves to write only when an
allowed path is also supplied. Normalize paths with `Path.resolve(strict=False)`
and compare them with `os.path.commonpath`; on cross-drive Windows input, convert
`ValueError` into a `WorkerError` whose code is `write_not_authorized` and whose
message says every allowed path must stay inside the work directory.

- [ ] **Step 4: Run focused and package tests**

```powershell
./.venv/Scripts/python -m unittest tests.test_routing tests.test_package -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit routing**

```powershell
git add -- src/codex_worker_dispatcher/routing.py tests/test_routing.py
git commit -m "feat: resolve scoped worker routes"
```

## Task 3: Build the Atomic Filesystem State Machine

**Files:**

- Create: `tests/test_state.py`
- Create: `src/codex_worker_dispatcher/state.py`

- [ ] **Step 1: Write failing state tests**

Test task ID acceptance and rejection, default state-root precedence, atomic
round trips, prompt persistence, terminal-state detection, sorted listing, and
the absence of temporary files after a successful write. Use temporary
directories and patch `CODEX_HOME` with `unittest.mock.patch.dict`.

The central round-trip test must assert:

```python
store = StateStore(root)
task_dir = store.create_task_dir("20260717-120000-a1b2c3d4")
store.write_prompt(task_dir, "inspect parser")
store.write_manifest(task_dir, {"task_id": task_dir.name, "status": "queued"})
self.assertEqual(store.read_prompt(task_dir), "inspect parser")
self.assertEqual(store.read_manifest(task_dir)["status"], "queued")
self.assertEqual(list(task_dir.glob("*.tmp")), [])
```

- [ ] **Step 2: Verify RED**

Run `./.venv/Scripts/python -m unittest tests.test_state -v`.

Expected: FAIL importing `codex_worker_dispatcher.state`.

- [ ] **Step 3: Implement `StateStore`**

Expose `TERMINAL_STATES`, `utc_now()`, `default_state_root()`, `valid_task_id()`,
and a `StateStore` with `task_dir`, `create_task_dir`, `read_manifest`,
`write_manifest`, `read_prompt`, `write_prompt`, and `list_manifests`.

Use UTF-8, `json.dumps(value, ensure_ascii=False, indent=2)`, a same-directory
temporary file, `flush`, `os.fsync`, and `os.replace`. Retry manifest reads up to
20 times with 25 ms between attempts. Reject symlinked task directories and task
IDs outside `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. Apply mode `0o700` to new POSIX
task directories.

- [ ] **Step 4: Verify GREEN**

Run `./.venv/Scripts/python -m unittest tests.test_state tests.test_routing tests.test_package -v`.

Expected: all tests pass.

- [ ] **Step 5: Commit state management**

```powershell
git add -- src/codex_worker_dispatcher/state.py tests/test_state.py
git commit -m "feat: persist atomic worker state"
```

## Task 4: Verify and Terminate Owned Processes Cross-Platform

**Files:**

- Create: `tests/test_process.py`
- Create: `src/codex_worker_dispatcher/process.py`

- [ ] **Step 1: Write failing process tests**

Test the current Python process identity, missing PID behavior, nonce hashing,
identity match and mismatch, and a real disposable child process that starts a
new process group. The termination test must launch:

```python
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(60)", "ownership-nonce"],
    creationflags=windows_detached_flags() if os.name == "nt" else 0,
    start_new_session=os.name != "nt",
)
identity = read_process_identity(child.pid)
terminate_owned_tree(child.pid, identity.start_marker, "ownership-nonce", grace_seconds=1.0)
self.assertTrue(wait_until_gone(child.pid, timeout_seconds=5.0))
```

Add a mismatch test that replaces the expected nonce and asserts
`WorkerError.code == "process_identity_mismatch"` while the child remains live;
clean that child in `finally` using its actual identity.

- [ ] **Step 2: Verify RED**

Run `./.venv/Scripts/python -m unittest tests.test_process -v`.

Expected: FAIL importing `codex_worker_dispatcher.process`.

- [ ] **Step 3: Implement platform adapters**

Create immutable `ProcessIdentity(pid, start_marker, command_line)` and public
functions named `ownership_hash`, `windows_detached_flags`,
`read_process_identity`, `identity_matches`, `wait_until_gone`, and
`terminate_owned_tree`. Keep their argument and return types identical to those
used by the test in Step 1; `terminate_owned_tree` defaults to a three-second
grace period.

Implement Windows identity through `powershell.exe -NoProfile` and
`Get-CimInstance Win32_Process`, returning creation date and command line as
compressed JSON. Terminate only after verification with `taskkill /PID <pid> /T`
and escalate to `/F` after the grace period. Implement Linux identity from
`/proc/<pid>/stat` field 22 and `/proc/<pid>/cmdline`. Implement macOS identity
with `ps -p <pid> -o lstart= -o command=`. On POSIX, verify `os.getpgid(pid) == pid`
before signaling the process group with `SIGTERM`, then `SIGKILL` after grace.

- [ ] **Step 4: Run the real process test twice**

```powershell
./.venv/Scripts/python -m unittest tests.test_process -v
./.venv/Scripts/python -m unittest tests.test_process -v
```

Expected: both runs pass and leave no test child alive.

- [ ] **Step 5: Commit process safety**

```powershell
git add -- src/codex_worker_dispatcher/process.py tests/test_process.py
git commit -m "feat: verify owned worker processes"
```

## Task 5: Supervise Fake and Real Codex Runners

**Files:**

- Create: `tests/test_supervisor.py`
- Create: `src/codex_worker_dispatcher/runner.py`
- Create: `src/codex_worker_dispatcher/supervisor.py`

- [ ] **Step 1: Write failing supervisor integration tests**

Build a helper that creates a task manifest with engine `fake`, a nonce hash,
workdir, delay, timeout, and empty process metadata. Start the supervisor as a
subprocess with the plaintext nonce. Test completion, non-zero fake exit,
cancellation, TTL timeout, and nonce rejection. Assert each terminal manifest,
runner disappearance, `events.jsonl`, `stderr.log`, and `last-message.txt`.

- [ ] **Step 2: Verify RED**

Run `./.venv/Scripts/python -m unittest tests.test_supervisor -v`.

Expected: FAIL because `codex_worker_dispatcher.supervisor` is missing.

- [ ] **Step 3: Implement the runner**

The fake engine sleeps in 50 ms intervals, writes one compact
`{"type":"fake.completed","message":"FAKE_RESULT_OK"}`-shaped event using the
actual prompt as the message when `fake_exit_code` is zero. A non-zero fake exit
writes `fake.failed` instead. Both paths write the final message and return
`fake_exit_code`. The real engine resolves `codex` with `shutil.which`,
builds the approved `codex exec` argument list without a shell, sends the prompt
on stdin, writes stdout to `events.jsonl`, stderr to `stderr.log`, and uses
`-o last-message.txt`. Add model, reasoning, and `--skip-git-repo-check` only when
the manifest requests them. Raise a `WorkerError` with code `codex_not_found`
before launch if the executable is absent.

- [ ] **Step 4: Implement the supervisor**

Parse `--task-dir` and `--ownership-nonce`, verify the manifest hash, record the
supervisor identity, launch the runner in a new process group, record runner
identity, and poll every 200 ms. On `cancel.request`, terminate the verified
runner tree and record `cancelled`. On TTL, terminate and record `timed_out`. On
normal exit, record `completed` only for code 0 and `failed` otherwise. Every
terminal write sets `updated_at`, `completed_at`, `exit_code`, and a useful error
for non-completion. An exception must make a best effort to reclaim the runner
and atomically record `failed`.

- [ ] **Step 5: Verify GREEN and orphan cleanup**

Run:

```powershell
./.venv/Scripts/python -m unittest tests.test_supervisor tests.test_process tests.test_state -v
```

Expected: all tests pass; a process search for the temporary test-root string
returns no live processes.

- [ ] **Step 6: Commit supervision**

```powershell
git add -- src/codex_worker_dispatcher/runner.py src/codex_worker_dispatcher/supervisor.py tests/test_supervisor.py
git commit -m "feat: supervise detached Codex runners"
```

## Task 6: Expose the Complete Lifecycle CLI

**Files:**

- Create: `tests/test_lifecycle.py`
- Create: `tests/test_cli.py`
- Create: `src/codex_worker_dispatcher/lifecycle.py`
- Modify: `src/codex_worker_dispatcher/cli.py`

- [ ] **Step 1: Write failing lifecycle tests**

Use a temporary workdir and state root. Exercise route, start plus wait plus
result, status reconciliation, controller wait timeout, cancel, reap, list, and
reap-stale dry-run/apply. The lifecycle helper must always clean any non-terminal
task in `addCleanup`.

The completion path must assert:

```python
started = start_task(prompt="FAKE_RESULT_OK", workdir=workdir, state_root=state_root, engine="fake", fake_delay_sec=0.1, timeout_sec=15)
waited = wait_task(started["task_id"], state_root, wait_timeout_sec=20)
self.assertEqual(waited["status"], "completed")
result = result_task(started["task_id"], state_root)
self.assertEqual(result["last_message"], "FAKE_RESULT_OK")
```

- [ ] **Step 2: Write failing black-box CLI tests**

Run the module in a subprocess and parse stdout as JSON. Test `route`, `start`,
`wait`, `result`, invalid task ID, unscoped explicit workspace write, and missing
task. Assert stdout contains one JSON value and errors have non-zero exit codes.

- [ ] **Step 3: Verify RED**

Run `./.venv/Scripts/python -m unittest tests.test_lifecycle tests.test_cli -v`.

Expected: FAIL importing lifecycle functions and parsing missing subcommands.

- [ ] **Step 4: Implement lifecycle actions**

Implement `route_task`, `start_task`, `status_task`, `wait_task`, `result_task`,
`cancel_task`, `reap_task`, `list_tasks`, and `reap_stale_tasks`. Start creates
schema version 2, a random 32-hex-character nonce, prompt, manifest, detached
supervisor, and recorded supervisor identity. Reconciliation marks completed
only when a non-empty final message and a `turn.completed` or `fake.completed`
event both exist; otherwise it marks an inactive non-terminal task orphaned.
Reap verifies and stops runner before supervisor. Stale cleanup is read-only
unless `apply=True`.

`wait_task` raises a `WorkerError` with code `wait_timeout` without changing the
manifest when its controller deadline expires. `cancel_task` may surface the
same error so the caller can choose `reap`.

- [ ] **Step 5: Implement argparse and JSON output**

Add the exact subcommands in the design. Each action accepts `--state-root` in
its own parser so invocation order is unambiguous. `start` requires exactly one
of `--prompt` and `--prompt-file`. Keep `--engine`, `--fake-delay-sec`, and
`--fake-exit-code` hidden from help but accepted for deterministic tests. Catch
`WorkerError` and print `to_dict()` to stdout with exit code 2; catch unexpected
exceptions as `internal_error` with exit code 1. Successful values include
`"ok": true` unless the value is the version response.

- [ ] **Step 6: Verify the lifecycle twice**

```powershell
./.venv/Scripts/python -m unittest tests.test_lifecycle tests.test_cli -v
./.venv/Scripts/python -m unittest discover -s tests -v
```

Expected: all tests pass and no worker remains in a non-terminal state.

- [ ] **Step 7: Commit lifecycle and CLI**

```powershell
git add -- src/codex_worker_dispatcher/lifecycle.py src/codex_worker_dispatcher/cli.py tests/test_lifecycle.py tests/test_cli.py
git commit -m "feat: expose worker lifecycle CLI"
```

## Task 7: Baseline, Write, and Pressure-Test the Agent Skill

**Files:**

- Create: `tests/skill-evals/scenarios.md`
- Create: `tests/skill-evals/baseline/`
- Create: `tests/skill-evals/with-skill/`
- Create: `tests/test_skill.py`
- Create: `skill/dispatching-codex-workers/SKILL.md`
- Create: `skill/dispatching-codex-workers/references/design.md`
- Create: `skill/dispatching-codex-workers/agents/openai.yaml`

- [ ] **Step 1: Write three pressure scenarios before the skill**

Scenarios must combine time pressure, an already-running worker, an attractive
shortcut, and explicit A/B/C choices. Cover: ending without collecting a result,
using write access without allowed paths, and killing a hung worker by broad
process name. Store the exact prompts in `tests/skill-evals/scenarios.md`.

- [ ] **Step 2: Run RED evaluations without exposing the skill**

Use three fresh read-only Codex worker sessions or native subagent sessions. Do
not mention the desired answer or the existing private skill. Save complete
responses verbatim under `tests/skill-evals/baseline/`. Record a short table of
observed missed invariants and exact rationalizations at the bottom of
`scenarios.md`.

Expected: at least one scenario fails a lifecycle, scoping, or verified-reap
invariant. If all pass, strengthen pressures and repeat before writing SKILL.md.

- [ ] **Step 3: Write the failing structural skill test**

`tests/test_skill.py` must assert valid frontmatter, exact skill name, a
description starting with `Use when`, fewer than 500 body lines, a lifecycle
close-loop section, `danger-full-access` prohibition, no removed local model
aliases or machine-specific absolute paths, and valid `openai.yaml` strings.

- [ ] **Step 4: Verify the structural test fails**

Run `./.venv/Scripts/python -m unittest tests.test_skill -v`.

Expected: FAIL because the public skill files do not exist.

- [ ] **Step 5: Write the minimal skill and reference**

The description must contain triggers only:

```yaml
---
name: dispatching-codex-workers
description: Use when bounded local Codex CLI work needs configurable model or reasoning, detached observable task state, TTLs, scoped write authorization, or verified worker recovery beyond native subagents.
---
```

The body must contain: overview, dispatch decision, access table, start/status/
wait/result/cancel/reap quick reference using `codex-worker`, a mandatory
close-loop checklist, red flags derived from baseline rationalizations, common
mistakes, and one complete read-only example. Move manifest fields and platform
process details to `references/design.md`.

Generate `agents/openai.yaml` with quoted values:

```yaml
interface:
  display_name: "Dispatching Codex Workers"
  short_description: "Cross-platform workers with safe recovery"
  default_prompt: "Use $dispatching-codex-workers to run bounded Codex workers with observable state and verified recovery."
```

- [ ] **Step 6: Validate structure and metadata**

Run:

```powershell
./.venv/Scripts/python -m unittest tests.test_skill -v
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
./.venv/Scripts/python (Join-Path $codexHome 'skills/.system/skill-creator/scripts/quick_validate.py') skill/dispatching-codex-workers
```

Expected: tests pass and validator prints a valid-skill result.

- [ ] **Step 7: Run GREEN pressure evaluations**

Run the same three scenarios in fresh sessions with only the public skill path
added. Save responses under `tests/skill-evals/with-skill/`. Each response must
choose the lifecycle-safe option, reject unscoped write, and refuse broad
process killing. If a new rationalization appears, add a specific counter to the
skill and rerun that scenario until it passes.

- [ ] **Step 8: Commit the tested skill**

```powershell
git add -- skill tests/test_skill.py tests/skill-evals
git commit -m "feat: add tested Codex worker skill"
```

## Task 8: Install and Remove the Bundled Skill Safely

**Files:**

- Create: `tests/test_installer.py`
- Create: `src/codex_worker_dispatcher/installer.py`
- Modify: `src/codex_worker_dispatcher/cli.py`

- [ ] **Step 1: Write failing installer tests**

Test fresh install, identical reinstall, conflict refusal, upgrade backup,
rollback after a simulated copy failure, owned uninstall, and refusal to remove
a directory without `.codex-worker-dispatcher.json`. All tests use a temporary
home and never touch the real `$HOME/.agents/skills`.

- [ ] **Step 2: Verify RED**

Run `./.venv/Scripts/python -m unittest tests.test_installer -v`.

Expected: FAIL importing installer functions.

- [ ] **Step 3: Implement atomic install and owned uninstall**

Locate package data under
`sysconfig.get_path("data")/share/codex-worker-dispatcher/skill/dispatching-codex-workers`,
falling back to the repository `skill/` directory for editable installs. Copy to
a sibling temporary directory, add a JSON ownership marker with package version
and source, then rename into place. With `upgrade=True`, rename the existing
destination to `.backup-<UTC timestamp>` first and restore it on failure.
Uninstall requires the marker and renames to a temporary tombstone before
recursive deletion.

- [ ] **Step 4: Add `skill install` and `skill uninstall` CLI commands**

Support `--target`, `--upgrade`, and `--yes`. Uninstall without `--yes` fails in
non-interactive mode with `invalid_arguments`; it must never prompt when stdout
is being used as JSON automation output.

- [ ] **Step 5: Verify installer and wheel contents**

```powershell
./.venv/Scripts/python -m pip install -i https://mirrors.aliyun.com/pypi/simple/ build
./.venv/Scripts/python -m unittest tests.test_installer tests.test_cli -v
./.venv/Scripts/python -m build
./.venv/Scripts/python -c "import zipfile, pathlib; wheel=next(pathlib.Path('dist').glob('*.whl')); names=zipfile.ZipFile(wheel).namelist(); assert any(name.endswith('dispatching-codex-workers/SKILL.md') for name in names)"
```

Expected: tests pass and the wheel assertion exits 0.

- [ ] **Step 6: Commit installer**

```powershell
git add -- src/codex_worker_dispatcher/installer.py src/codex_worker_dispatcher/cli.py tests/test_installer.py pyproject.toml
git commit -m "feat: install bundled worker skill"
```

## Task 9: Complete Public Documentation and Release Hygiene

**Files:**

- Create: `README.md`
- Create: `README.zh-CN.md`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`
- Create: `LICENSE`
- Create: `tests/test_public_release.py`
- Modify: `PROJECT_STATUS.md`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write the failing public-release audit**

Scan distributable source, skill, workflow, and public-documentation files and
fail on local absolute paths, personal email addresses, token prefixes, the
removed local model aliases, or files
named `.env`, `auth.json`, `manifest.json`, `prompt.txt`, `events.jsonl`,
`stderr.log`, or `last-message.txt`. Assert README sections for prerequisites,
installation, read/write examples, lifecycle recovery, privacy, supported
platforms, uninstall, license, and unofficial status. Assert the Apache-2.0
license signature.

- [ ] **Step 2: Verify RED**

Run `./.venv/Scripts/python -m unittest tests.test_public_release -v`.

Expected: FAIL because public documentation and LICENSE are absent.

- [ ] **Step 3: Write English and Chinese READMEs**

Both versions must document Python 3.10+, separately installed and authenticated
Codex CLI, `pipx` installation from GitHub, virtual-environment fallback, skill
installation, route preview, read-only start, scoped-write start, polling,
result collection, cancellation, verified reap, stale dry-run/apply, state-file
privacy, upgrade, uninstall, platform matrix, limitations, Apache-2.0, and the
unofficial OpenAI disclaimer. Examples use `$HOME`/`~` and neutral `/path/to/repo`
paths only.

Add `readme = "README.md"` to `[project]` in `pyproject.toml` after README exists.

- [ ] **Step 4: Add contribution, security, and license documents**

Use the unmodified Apache License 2.0 text. `SECURITY.md` must tell reporters not
to open public issues for vulnerabilities and to use GitHub private vulnerability
reporting. `CONTRIBUTING.md` must require a failing test first, all-platform-safe
process changes, `./.venv/Scripts/python -m unittest discover -s tests -v`, and no real prompts
or worker state in fixtures.

- [ ] **Step 5: Run the public audit and full suite**

```powershell
./.venv/Scripts/python -m unittest tests.test_public_release -v
./.venv/Scripts/python -m unittest discover -s tests -v
git diff --check
git status --short
```

Expected: tests pass, diff check is silent, and status lists only intended
documentation and test files.

- [ ] **Step 6: Commit public documentation**

```powershell
git add -- README.md README.zh-CN.md SECURITY.md CONTRIBUTING.md LICENSE PROJECT_STATUS.md pyproject.toml tests/test_public_release.py
git commit -m "docs: prepare public worker release"
```

## Task 10: Add Cross-Platform CI, Publish a Sanitized Snapshot, and Verify the Public Repository

**Files:**

- Create: `.github/workflows/ci.yml`
- Modify: `PROJECT_STATUS.md`

- [ ] **Step 1: Add the CI workflow**

Use `actions/checkout@v4` and `actions/setup-python@v5`. Define a matrix across
`windows-latest`, `ubuntu-latest`, and `macos-latest` with Python `3.10` and
`3.14`. Install `build`, install the package editable, run unittest discovery,
build sdist and wheel, install the wheel into a clean temporary virtual
environment, run `codex-worker --version`, install the skill into a temporary
target, and run the public-release audit. Upload `dist/*` only from Ubuntu
Python 3.14.

- [ ] **Step 2: Validate workflow syntax and run final local checks**

```powershell
./.venv/Scripts/python -m unittest discover -s tests -v
./.venv/Scripts/python -m build
./.venv/Scripts/python -m venv .venv-smoke
./.venv-smoke/Scripts/python -m pip install --no-index --find-links dist codex-worker-dispatcher
./.venv-smoke/Scripts/codex-worker --version
./.venv-smoke/Scripts/codex-worker skill install --target "$env:TEMP/codex-worker-skill-smoke"
./.venv/Scripts/python -m unittest tests.test_public_release tests.test_skill -v
git diff --check
git status -sb
```

Expected: all tests pass, wheel installs without network, version JSON is
correct, skill installation returns success JSON, diff check is silent, and the
working tree contains only the intended CI/status change.

- [ ] **Step 3: Review the complete release diff**

Run:

```powershell
git diff 7614220..HEAD --stat
git log --oneline --decorate
./.venv/Scripts/python -m unittest tests.test_public_release -v
```

Expected: the release diff is intentional, and the tracked-text audit finds no
private path, credential data, email address, or removed local model alias.

- [ ] **Step 4: Commit CI and final status**

```powershell
git add -- .github/workflows/ci.yml PROJECT_STATUS.md
git commit -m "ci: verify dispatcher across platforms"
```

- [ ] **Step 5: Export and reverify a sanitized single-commit snapshot**

The development repository is an input only. Do not add a public remote to it
and never push its history. Require a clean verified working tree, export only
the committed tracked tree, and initialize an unrelated repository in a new
temporary directory:

```powershell
$dirty = git status --porcelain
if ($dirty) { throw "Development working tree must be clean before snapshot export." }

$snapshotRoot = Join-Path $env:TEMP "codex-worker-dispatcher-public-$([guid]::NewGuid())"
$snapshotTar = "$snapshotRoot.tar"
New-Item -ItemType Directory -Path $snapshotRoot | Out-Null
git archive --format=tar -o "$snapshotTar" HEAD
tar -xf "$snapshotTar" -C "$snapshotRoot"

git -C $snapshotRoot init -b main
git -C $snapshotRoot add -A
git -C $snapshotRoot commit -m "Release v0.1.0"
$snapshotCommitCount = [int](git -C $snapshotRoot rev-list --count main)
if ($snapshotCommitCount -ne 1) { throw "Public snapshot must contain exactly one commit." }
```

Create a fresh virtual environment inside that snapshot, then run its own audit,
full test suite, and package build. The editable install must point at the
snapshot, not at the development worktree:

```powershell
python -m venv (Join-Path $snapshotRoot ".venv")
$snapshotPython = if ($IsWindows) {
    Join-Path $snapshotRoot ".venv/Scripts/python.exe"
} else {
    Join-Path $snapshotRoot ".venv/bin/python"
}

Push-Location $snapshotRoot
try {
    & $snapshotPython -m pip install --upgrade pip setuptools wheel build
    & $snapshotPython -m pip install -e .
    & $snapshotPython -m unittest discover -s tests -v
    & $snapshotPython -m unittest tests.test_public_release tests.test_skill -v
    & $snapshotPython -m build
    git diff --check
    if (git status --porcelain --untracked-files=no) {
        throw "Snapshot verification modified tracked files."
    }
} finally {
    Pop-Location
}
```

Expected: one unrelated public root commit, passing audit/tests/build, and no
tracked changes. The development commit graph is absent from `$snapshotRoot`.

- [ ] **Step 6: Create the PUBLIC repository, enable private reporting, and push only the snapshot**

Confirm `gh auth status` and confirm `holdonyb/codex-worker-dispatcher` does not
already exist. Create the empty PUBLIC repository without `--source` or
`--push`, enable private vulnerability reporting through the official GitHub
REST endpoint, verify it reports `enabled: true`, and only then push from the
temporary snapshot repository:

```powershell
gh auth status
gh repo create holdonyb/codex-worker-dispatcher --public --description "Cross-platform, observable, and safely reclaimable local workers for OpenAI Codex CLI."
gh api --method PUT repos/holdonyb/codex-worker-dispatcher/private-vulnerability-reporting
$privateReportingEnabled = gh api repos/holdonyb/codex-worker-dispatcher/private-vulnerability-reporting --jq '.enabled'
if ($privateReportingEnabled -ne "true") { throw "Private vulnerability reporting is not enabled." }

git -C $snapshotRoot remote add origin https://github.com/holdonyb/codex-worker-dispatcher.git
git -C $snapshotRoot push -u origin main
gh repo edit holdonyb/codex-worker-dispatcher --enable-issues --enable-wiki=false --add-topic codex --add-topic codex-cli --add-topic agent-skills --add-topic ai-agents --add-topic developer-tools --add-topic python --add-topic cross-platform
```

Expected: repository URL is `https://github.com/holdonyb/codex-worker-dispatcher`,
private vulnerability reporting is enabled, and public `main` contains only the
verified single snapshot commit. The local development repository has no public
remote and none of its pre-audit commits were pushed.

- [ ] **Step 7: Monitor GitHub Actions to a terminal result**

```powershell
$runId = gh run list --repo holdonyb/codex-worker-dispatcher --workflow ci.yml --limit 1 --json databaseId --jq '.[0].databaseId'
gh run watch $runId --repo holdonyb/codex-worker-dispatcher --exit-status
gh run view $runId --repo holdonyb/codex-worker-dispatcher --json conclusion,url,jobs
```

Expected: every Windows, Ubuntu, and macOS matrix job concludes `success`. If a
job fails, inspect its logs, reproduce or isolate the failure, add a failing
test when behavior is wrong, fix it in the development tree, regenerate and
reverify a sanitized snapshot commit, push only from the snapshot repository,
and watch the replacement run. Never push the development branch.

- [ ] **Step 8: Create release `v0.1.0` only after green CI**

```powershell
gh release create v0.1.0 --repo holdonyb/codex-worker-dispatcher --target main --title "v0.1.0" --notes "Initial public release of the cross-platform Codex worker dispatcher and Agent Skill."
gh repo view holdonyb/codex-worker-dispatcher --json nameWithOwner,visibility,url,licenseInfo,repositoryTopics
```

Expected: PUBLIC visibility, detected Apache-2.0 license, configured topics, and
a visible `v0.1.0` release.

- [ ] **Step 9: Record completion without exposing development history**

Update the snapshot copy of `PROJECT_STATUS.md` with the repository URL,
release URL, passing CI run URL, validation date, and no unresolved blockers.
Rerun the public audit, then commit and push this public documentation update
from `$snapshotRoot`. Mirror the final status into the local development copy
for recovery context, but never push the development branch to the public
remote:

```powershell
Push-Location $snapshotRoot
try {
    & $snapshotPython -m unittest tests.test_public_release -v
    git add -- PROJECT_STATUS.md
    git commit -m "docs: record public release status"
    git push
} finally {
    Pop-Location
}
```

## Final Verification Gate

Before claiming completion, invoke `verification-before-completion` and collect
fresh evidence for all of the following:

- `./.venv/Scripts/python -m unittest discover -s tests -v` passes locally;
- wheel build and clean-environment install pass;
- skill validator passes;
- public-release audit passes;
- no task remains non-terminal in the test state roots;
- `git status -sb` is clean;
- GitHub repository is PUBLIC;
- Apache-2.0 is detected;
- GitHub private vulnerability reporting returns `enabled: true`;
- the public root commit came from the sanitized snapshot and no development
  history was pushed;
- every GitHub Actions matrix job is green;
- release `v0.1.0` exists.
