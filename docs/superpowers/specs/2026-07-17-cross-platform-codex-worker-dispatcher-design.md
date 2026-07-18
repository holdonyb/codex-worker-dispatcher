# Cross-Platform Codex Worker Dispatcher Design

**Date:** 2026-07-17

**Status:** Implemented and validated for `v0.1.0`

**Repository:** `holdonyb/codex-worker-dispatcher`

**Visibility:** Public

**License:** Apache-2.0

## 1. Goal

Extract the existing local `dispatching-codex-workers` skill into a standalone,
public repository and replace its Windows-only runtime with a Python 3.10+
implementation that works on native Windows, macOS, and Linux.

The project must preserve the useful behavior of the existing controller:

- route bounded Codex CLI work;
- start workers without tying their lifetime to the launching shell;
- persist observable task state on disk;
- enforce task TTLs;
- support status, wait, result, cancellation, verified reap, listing, and stale cleanup;
- authorize workspace writes only when write intent and explicit allowed paths are both present;
- avoid `danger-full-access` entirely;
- package the operating instructions as an installable Codex Agent Skill.

Success means a new user can install the CLI and skill from the public GitHub
repository, run the same lifecycle commands on all three operating-system
families, and see the lifecycle test suite pass in GitHub Actions on Windows,
Ubuntu, and macOS.

## 2. Public Release Decision

The current local implementation is suitable for open-sourcing after
sanitization. The audit found no credentials, tokens, account identifiers, or
private message content. Two local assumptions must not be published as public
defaults:

1. machine-specific absolute worktree paths;
2. hard-coded local model nicknames and their private-looking model IDs.

The public version will omit a model argument by default so Codex uses the
caller's current configuration. `--model` remains an optional opaque string for
users who explicitly want an override. The repository will describe itself as
an unofficial community project that is not affiliated with or endorsed by
OpenAI.

The wrapper invokes an independently installed Codex CLI and does not
redistribute Codex source or binaries. The new project code will be licensed
under Apache-2.0. Repository history, generated test state, prompts, model
outputs, credentials, and local Codex configuration must never be included in
the initial public commit.

This is an engineering and provenance assessment, not a legal opinion.

## 3. Intended Users and Examples

### Read-only inspection

A parent agent starts a worker to inspect a parser and report likely failure
modes. The worker inherits the user's configured model, receives a read-only
sandbox, runs with a ten-minute TTL, and leaves a result that the parent can
collect later.

### Scoped implementation

A parent agent starts a write worker for `src/parser`. The controller authorizes
`workspace-write` only because the task has explicit write intent and an allowed
path that resolves inside the selected worktree. The allowed-path list is an
auditable assignment contract; it is not represented as an operating-system
subdirectory sandbox.

### Recovery

A worker exceeds its TTL or no longer responds to cooperative cancellation. The
supervisor first requests graceful termination, then stops only the recorded
process group or process tree. A later `reap` also verifies process identity
before forceful termination, preventing a recycled PID from being killed.

### Skill installation

A user installs the package with `pipx` or a virtual environment, then runs a
CLI subcommand that copies the bundled skill to `$HOME/.agents/skills`. The
installer refuses to overwrite an unrelated existing directory without an
explicit upgrade option.

## 4. Considered Approaches

### A. Keep PowerShell Core as the implementation

This produces the smallest diff but requires `pwsh` on macOS and Linux and still
needs separate replacements for CIM, `Win32_Process.Create`, and Windows process
tree traversal. It would make cross-platform support conditional on a runtime
that is not normally installed on Unix systems.

### B. Python core with an installable Agent Skill

This is the selected approach. Python 3.10+ provides one controller and
supervisor implementation with small platform adapters for process inspection
and termination. It supports packaging, `pipx`, standard-library tests, and a
single public CLI contract without requiring PowerShell outside Windows.

### C. Rust single-binary rewrite

Rust would produce attractive standalone binaries and strong process control,
but it adds compilation, release artifacts, signing, and multi-architecture
distribution before the public workflow is validated. It is outside the first
release scope.

## 5. Repository Structure

```text
codex-worker-dispatcher/
├── .github/workflows/ci.yml
├── docs/superpowers/
│   ├── plans/
│   └── specs/
├── skill/dispatching-codex-workers/
│   ├── agents/openai.yaml
│   ├── references/design.md
│   └── SKILL.md
├── src/codex_worker_dispatcher/
│   ├── __init__.py
│   ├── __main__.py
│   ├── cli.py
│   ├── installer.py
│   ├── lifecycle.py
│   ├── process.py
│   ├── runner.py
│   ├── routing.py
│   ├── state.py
│   └── supervisor.py
├── tests/
│   ├── test_cli.py
│   ├── test_lifecycle.py
│   ├── test_process.py
│   ├── test_routing.py
│   └── test_skill.py
├── .gitignore
├── AGENTS.md
├── CONTRIBUTING.md
├── LICENSE
├── PROJECT_STATUS.md
├── README.md
├── README.zh-CN.md
├── SECURITY.md
└── pyproject.toml
```

Production modules stay focused: routing decides permissions, state owns atomic
manifest persistence, process owns platform-specific identity and termination,
lifecycle owns actions, supervisor owns one running worker, installer owns skill
deployment, runner owns the concrete Codex subprocess, and CLI owns argument and
JSON output handling.

## 6. CLI Contract

The console entry point is `codex-worker`.

```text
codex-worker route
codex-worker start
codex-worker status TASK_ID
codex-worker wait TASK_ID
codex-worker result TASK_ID
codex-worker cancel TASK_ID
codex-worker reap TASK_ID
codex-worker list
codex-worker reap-stale
codex-worker skill install
codex-worker skill uninstall
```

Every operational action writes exactly one JSON object to stdout. Argparse
`--help` output remains human-readable. Diagnostics go to stderr. Failures
return a non-zero exit code and a stable JSON error envelope.

### Routing inputs

- `--complexity auto|simple|standard|complex|hard|extreme`
- `--intent auto|read|write`
- `--sandbox auto|read-only|workspace-write`
- repeatable `--allowed-path PATH`
- optional `--model MODEL`
- `--reasoning auto|low|medium|high|xhigh`

When complexity is automatic, explicit write intent resolves to `standard` and
all other work resolves to `simple`. Automatic reasoning maps as follows:

| Complexity | Reasoning |
|---|---|
| simple | low |
| standard | medium |
| complex | high |
| hard | xhigh |
| extreme | xhigh |

No model is selected automatically. When `--reasoning` is explicitly supplied,
the value is forwarded through Codex configuration. The first public release
uses the documented effort values above rather than local-only levels.

### Write authorization

The resolved sandbox is `workspace-write` only when:

1. resolved intent is `write`;
2. at least one allowed path is supplied;
3. every allowed path resolves inside the selected work directory.

An explicit `workspace-write` request that does not satisfy all three
conditions fails. `danger-full-access` is not accepted by the parser.

## 7. Runtime Architecture and Data Flow

1. `start` validates the prompt, work directory, route, timeout, and optional
   task ID.
2. The controller creates a private task directory and atomically writes the
   prompt and an initial manifest.
3. The controller launches `python -m codex_worker_dispatcher.supervisor` as a
   detached process with the task directory and a random ownership nonce in its
   command line.
4. The supervisor records its process identity and starts
   `python -m codex_worker_dispatcher.runner` in a dedicated process group. The
   runner command line carries the same nonce and starts `codex exec` with stdin
   from `prompt.txt`.
5. Codex JSONL events go to `events.jsonl`, diagnostics go to `stderr.log`, and
   the final response goes to `last-message.txt`.
6. The supervisor polls for worker exit, `cancel.request`, and TTL expiration.
7. It records one terminal state with an atomic manifest replacement.
8. Controller actions read or reconcile persisted state without requiring the
   original parent process to remain alive.

The Codex invocation includes `exec`, `--ephemeral`, `--json`, `--color never`,
the resolved sandbox, working directory, output-last-message path, and `-` for
stdin. It includes model and reasoning overrides only when resolved values are
present. The existing `--skip-git-repo-check` behavior remains available as an
explicit CLI flag rather than an unconditional default.

## 8. State Model

State defaults to `$CODEX_HOME/worker-runs`; if `CODEX_HOME` is unset, it uses
`~/.codex/worker-runs`. Each task directory contains:

```text
manifest.json
prompt.txt
events.jsonl
stderr.log
last-message.txt
cancel.request       # only after cancellation is requested
```

Manifest schema version 2 includes task identity, timestamps, normalized
working directory, route, allowed paths, timeout, process IDs, process start
identities, ownership nonce hash, exit code, terminal timestamps, and error.
The nonce itself is present in supervisor and runner command lines; the manifest
stores its hash for process-identity comparison.

Terminal states are:

- `completed`
- `failed`
- `timed_out`
- `cancelled`
- `reaped`
- `orphaned`

Manifest updates use a temporary file in the same directory followed by
`os.replace`, making state transitions atomic on all supported systems.

## 9. Cross-Platform Process Safety

### Windows

The platform adapter queries process creation time and command line through the
built-in Windows CIM interface. It verifies PID, creation time, task directory,
and ownership nonce before using Windows tree termination for forced cleanup.
The implementation invokes Windows PowerShell only for the CIM query; it does
not require PowerShell 7.

### Linux

The adapter reads process start identity and command line from `/proc`. The
supervisor gives the worker a dedicated process group. Cancellation and timeout
send `SIGTERM`, wait for a bounded grace period, then send `SIGKILL` to that
verified group if required.

### macOS

The adapter obtains start identity and command line through `ps`, since macOS
does not expose Linux `/proc`. Termination uses the same dedicated POSIX process
group and `SIGTERM`/`SIGKILL` sequence as Linux.

No action kills by executable image name, broad command substring, or
repository-wide process match.

## 10. Reconciliation and Error Handling

`status`, `wait`, and `result` reconcile stale non-terminal manifests. If no
recorded process remains, a final response plus a completion event yields
`completed`; otherwise the task becomes `orphaned` with a diagnostic reason.

Controller errors use stable codes such as:

- `invalid_arguments`
- `invalid_task_id`
- `task_not_found`
- `task_exists`
- `write_not_authorized`
- `process_identity_mismatch`
- `codex_not_found`
- `wait_timeout`
- `install_conflict`

A controller wait timeout does not mutate worker state. A worker TTL does mutate
the task to `timed_out` after reclaiming its process tree.

## 11. Skill Design

The bundled `dispatching-codex-workers` skill remains concise and refers to
`codex-worker --help` for exhaustive flags. Its trigger covers bounded local
Codex CLI work that needs configurable reasoning, observable detached state,
TTLs, scoped write authorization, or recovery beyond native subagents.

The skill requires the parent agent to:

1. dispatch only independent, bounded tasks;
2. assign non-overlapping write scopes;
3. store every task ID;
4. drive every worker to a terminal state;
5. collect results and inspect failures;
6. review diffs and run parent-level validation;
7. cancel or reap outstanding workers before ending.

The public skill contains no machine-specific paths or model aliases.

## 12. Installation and Documentation

The initial release supports installation from GitHub without publishing to
PyPI:

```bash
pipx install git+https://github.com/holdonyb/codex-worker-dispatcher.git
codex-worker skill install
```

`python -m pip install .` inside a virtual environment is documented as the
fallback. README documentation will cover prerequisites, Codex authentication,
quick starts, all lifecycle actions, the state directory, privacy implications,
failure recovery, upgrades, uninstalling, platform limitations, and the
unofficial-project disclaimer. English is the primary README and a complete
Chinese translation lives in `README.zh-CN.md`.

The skill installer targets `$HOME/.agents/skills/dispatching-codex-workers` by
default. Installation is atomic. It refuses conflicts unless `--upgrade` is
provided; upgrade first creates a timestamped backup alongside the destination.

## 13. Verification Strategy

Implementation follows test-driven development.

### Unit tests

- routing matrix and explicit overrides;
- allowed-path normalization and escape rejection;
- task ID validation;
- atomic manifest writes;
- JSON error envelopes;
- process identity comparison;
- skill metadata and machine-specific-string audit.

### Lifecycle integration tests

A deterministic fake engine exercises:

- completion and result collection;
- cooperative cancellation;
- TTL timeout and descendant cleanup;
- verified force reap;
- PID/identity mismatch refusal;
- stale metadata reconciliation;
- stale cleanup dry-run and explicit apply;
- list output;
- wait timeout without worker mutation.

### Skill behavior tests

Before editing the skill, baseline agents receive realistic worker-dispatch
requests without the skill. Their missed lifecycle or permission requirements
are recorded. The same scenarios are repeated with the skill installed, and
the skill is refined until the parent consistently closes the lifecycle and
preserves scoped access.

### Continuous integration

GitHub Actions runs the complete standard-library test suite on:

- `windows-latest`
- `ubuntu-latest`
- `macos-latest`

using Python 3.10 and Python 3.14.
Tests use the fake engine and therefore require neither Codex credentials nor
API access. A packaging smoke test builds a wheel, installs it into a clean
environment, runs `codex-worker --help`, installs the skill into a temporary
home, and validates the resulting skill directory.

The public snapshot passed all six Windows, Ubuntu, and macOS matrix jobs for
Python 3.10 and 3.14 in
[GitHub Actions run 29630973235](https://github.com/holdonyb/codex-worker-dispatcher/actions/runs/29630973235)
before release `v0.1.0` was created.

## 14. Public Repository Configuration

The GitHub repository was created under `holdonyb` with public visibility,
Issues and private vulnerability reporting enabled, and this description:

> Cross-platform, observable, and safely reclaimable local workers for OpenAI
> Codex CLI.

Topics:

- `codex`
- `codex-cli`
- `agent-skills`
- `ai-agents`
- `developer-tools`
- `python`
- `cross-platform`

The first public history begins with sanitized project files only. No source
copy from the private local skill is committed before its machine-specific and
private routing assumptions are replaced.

## 15. Non-Goals for v0.1.0

- a hosted worker service;
- remote execution or distributed scheduling;
- a GUI;
- automatic provider or model discovery;
- API key storage or authentication management;
- a stronger OS-level sandbox than Codex itself supplies;
- PyPI publication;
- compiled standalone binaries;
- compatibility with Python versions older than 3.10.
