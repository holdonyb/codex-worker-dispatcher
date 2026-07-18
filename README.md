# Codex Worker Dispatcher

[简体中文](README.zh-CN.md)

`codex-worker-dispatcher` is a cross-platform controller for bounded local Codex
CLI jobs. It starts detached workers, records observable state, enforces a task
TTL, and provides task-specific cancellation and identity-verified recovery.
Normal operational commands write one JSON object to stdout. Argparse
`--help` output is human-readable text.

## Prerequisites

- Python 3.10+ (3.10 or newer).
- A separately installed `codex` CLI available on `PATH`.
- The Codex CLI must already be authenticated and usable in the selected
  working directory. This package does not install Codex or manage credentials.
- `pipx` is recommended. A normal Python virtual environment also works.

Confirm both tools before installing:

```console
python --version
codex --version
```

## Installation

Install the latest version directly from GitHub with `pipx`:

```console
pipx install git+https://github.com/holdonyb/codex-worker-dispatcher.git
codex-worker --version
codex-worker skill install
```

The skill is installed by default at
`~/.agents/skills/dispatching-codex-workers`. Restart or reload your agent tool
if it does not discover newly installed skills automatically.

As a virtual-environment fallback:

```console
python -m venv .venv
./.venv/bin/python -m pip install git+https://github.com/holdonyb/codex-worker-dispatcher.git
./.venv/bin/codex-worker skill install
```

On Windows, use `./.venv/Scripts/python` and
`./.venv/Scripts/codex-worker` for the last two commands.

## Route preview

Preview the resolved intent, sandbox, model, and reasoning before starting a
worker. A preview does not create a task:

```console
codex-worker route --prompt "Inspect the parser and report likely edge cases." --workdir /path/to/repo --intent read
```

The model is not overridden unless `--model` is supplied, so the worker uses
the caller's current Codex configuration by default.

## Read-only example

Start a bounded read-only task and retain the `task_id` from its JSON response:

```console
codex-worker start --prompt "Inspect the parser and report likely edge cases. Do not modify files." --workdir /path/to/repo --intent read --sandbox read-only --timeout-sec 600
codex-worker status TASK_ID
codex-worker wait TASK_ID --wait-timeout-sec 120
codex-worker result TASK_ID
```

The worker TTL (`--timeout-sec`) and the controller's wait deadline
(`--wait-timeout-sec`) are different. A wait timeout does not stop the worker.

## Scoped-write example

Write access requires explicit write intent, `workspace-write`, and at least one
allowed path inside the work directory:

```console
codex-worker start --prompt "Update the parser and its focused tests." --workdir /path/to/repo --intent write --sandbox workspace-write --allowed-path /path/to/repo/src/parser --allowed-path /path/to/repo/tests --timeout-sec 600
codex-worker wait TASK_ID --wait-timeout-sec 120
codex-worker result TASK_ID
```

Allowed paths are an auditable assignment contract. They do not create an
operating-system subdirectory sandbox. Review every worker diff and run the
parent project's validation before accepting changes. Unrestricted
`danger-full-access` workers are prohibited.

## Lifecycle and recovery

Keep every returned task ID until it reaches `completed`, `failed`,
`timed_out`, `cancelled`, `reaped`, or `orphaned`, then collect its result.

```console
codex-worker list
codex-worker status TASK_ID
codex-worker wait TASK_ID --wait-timeout-sec 120
codex-worker result TASK_ID
codex-worker cancel TASK_ID --wait-timeout-sec 30
codex-worker reap TASK_ID
```

Use `cancel` first for cooperative shutdown. Use `reap` only for a specific
stuck task: it verifies the recorded process start identity, task directory,
role, and ownership nonce before terminating the owned process tree or group.
It never recovers workers by executable name or broad process matching.

Stale cleanup is a dry run unless `--apply` is present:

```console
codex-worker reap-stale --older-than-sec 3600
codex-worker reap-stale --older-than-sec 3600 --apply
```

## State privacy

Task state defaults to `$CODEX_HOME/worker-runs`, or
`~/.codex/worker-runs` when `CODEX_HOME` is unset. A task directory can contain
the submitted prompt, work directory, allowed paths, process metadata, event
output, diagnostics, and the final message. Treat that directory as private:
do not commit it, attach it to issues, or share it without reviewing and
redacting its contents. Use `--state-root` when an isolated location is needed.

The dispatcher does not upload state itself. The independently installed Codex
CLI still communicates according to the user's Codex configuration and terms.

## Supported platforms

| Platform | Process handling | CI target |
|---|---|---|
| Windows 11 / Windows Server | Native detached process tree, identity checks, and no extra worker console window | `windows-latest` |
| macOS | Detached POSIX process group with bounded identity rechecks | `macos-latest` |
| Linux | Detached process group with `/proc` identity and pidfd-backed signaling when available | `ubuntu-latest` |

Python 3.10 through 3.14 is the supported interpreter range. These Task 10 CI targets
passed on 2026-07-18 in the six-job
[GitHub Actions matrix](https://github.com/holdonyb/codex-worker-dispatcher/actions/runs/29630973235).
The verified source is published as
[v0.1.0](https://github.com/holdonyb/codex-worker-dispatcher/releases/tag/v0.1.0).

## Public release provenance

The public `main` branch starts from a single-commit sanitized snapshot of a
locally verified tracked tree. The snapshot is audited, tested, and built again
in a clean temporary repository before its first push. The development history is never pushed to the public repository.

## Upgrade and uninstall

Upgrade the CLI and then upgrade its managed skill copy:

```console
pipx upgrade codex-worker-dispatcher
codex-worker skill install --upgrade
```

`skill install --upgrade` keeps a timestamped backup when it replaces an
existing directory. Review and remove backups after confirming the upgrade.

Uninstall the owned skill explicitly, then remove the CLI:

```console
codex-worker skill uninstall --yes
pipx uninstall codex-worker-dispatcher
```

The skill uninstaller refuses to delete a directory without the dispatcher's
valid ownership marker. Virtual-environment users can remove their environment
after uninstalling the skill.

## Limitations

- This is an alpha release for local, bounded Codex CLI work; it is not a
  remote queue, hosted service, or multi-user scheduler.
- Codex itself, authentication, model availability, usage limits, and network
  access remain the user's responsibility.
- Allowed paths express delegated scope but are not an OS-level subdirectory
  sandbox.
- A parent agent or operator must retain task IDs, close every lifecycle, review
  outputs and diffs, and run project-level validation.
- Recovery deliberately refuses to terminate a process when ownership cannot
  be proved.

## License

Project code and documentation are licensed under [Apache-2.0](LICENSE). The
Codex CLI is a separate product and is not redistributed by this repository.

## Unofficial project

This is an unofficial community project. It is not affiliated with, endorsed
by, or sponsored by OpenAI. “OpenAI” and “Codex” are used only to identify the
separately installed tool with which this project interoperates.
