# Public sharing guide

This page is the short version you can send to someone else.

## What this repository is

`codex-worker-dispatcher` is a public Codex Skill plus a local runtime for
bounded Codex CLI worker dispatch. It is meant for local agent handoff with
task IDs, lifecycle control, cancellation, and recovery.

Repository:

- https://github.com/holdonyb/codex-worker-dispatcher

Verified release:

- https://github.com/holdonyb/codex-worker-dispatcher/releases/tag/v0.1.1

Latest green CI matrix:

- https://github.com/holdonyb/codex-worker-dispatcher/actions/runs/29653928879

## Can this be public?

Yes. The public repository was prepared as a sanitized public release:

- the repository starts from a public-safe snapshot
- development-only history was not pushed
- the public tree was audited, rebuilt, and tested again before release
- GitHub CI passed on Windows, macOS, and Linux for Python 3.10 and 3.14

This is still an unofficial community project. It is not affiliated with or
endorsed by OpenAI.

## What people should install

Most users should install the runtime and then let it install the managed Skill:

```console
pipx install git+https://github.com/holdonyb/codex-worker-dispatcher.git
codex-worker skill install
```

That installs the Skill to:

- `$CODEX_HOME/skills/dispatching-codex-workers`, or
- `~/.codex/skills/dispatching-codex-workers` if `CODEX_HOME` is unset

Users who only want to inspect the Skill source can clone the repository
directly into their Codex skills directory instead.

## What to send people

For public sharing, use these links:

- Repo: https://github.com/holdonyb/codex-worker-dispatcher
- Release: https://github.com/holdonyb/codex-worker-dispatcher/releases/tag/v0.1.1
- English README: https://github.com/holdonyb/codex-worker-dispatcher#readme
- Chinese README: https://github.com/holdonyb/codex-worker-dispatcher/blob/main/README.zh-CN.md

## Platform status

- Windows: supported, and worker launches avoid opening an extra console window
- macOS: supported
- Linux: supported

## Scope and limits

This project is for bounded local Codex CLI work. It is not:

- a hosted service
- a remote queue
- a multi-user scheduler
- an OS-level subdirectory sandbox
