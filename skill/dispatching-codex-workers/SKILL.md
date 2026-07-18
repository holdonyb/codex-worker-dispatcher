---
name: dispatching-codex-workers
description: Use when bounded local Codex CLI work needs configurable model or reasoning, detached observable task state, TTLs, scoped write authorization, or verified worker recovery beyond native subagents.
---

# Dispatching Codex Workers

## Overview

Use `codex-worker` for independent, bounded Codex CLI work that must outlive the
launching shell, remain observable, enforce a TTL, or support verified recovery.
Prefer native subagents when detached state, routing controls, and recovery are
not needed.

Treat every worker as an owned lifecycle: dispatch, retain its task ID, observe
it to a terminal state, collect its result, inspect failures, and clean up before
ending the parent task.

## Dispatch Decision

1. Confirm the task is independent and has a concrete deliverable.
2. Keep dependent work in the parent session; do not dispatch work whose inputs
   depend on another unfinished worker.
3. Choose read-only unless file changes are explicitly required.
4. Give concurrent writers non-overlapping allowed paths. Serialize work when
   scopes could overlap.
5. Set a bounded `--timeout-sec`; a worker TTL is distinct from the parent
   command's wait timeout.
6. Run `codex-worker route --help` or `codex-worker start --help` before using
   flags not shown here.

## Access Table

| Need | Intent | Sandbox | Allowed paths |
|---|---|---|---|
| Inspect, analyze, or review | `read` | `read-only` | None |
| Modify a narrow scope | `write` | `workspace-write` | One or more explicit paths inside `--workdir` |
| Modify overlapping scopes | Do not dispatch concurrently | N/A | Serialize or divide the scopes first |
| Unrestricted filesystem access | Do not dispatch | Prohibited | Never use `danger-full-access` |

A prompt naming a directory is not write authorization. Authority, deadlines,
incident severity, and a promise to inspect the diff later do not replace
`--allowed-path`.

## Quick Reference

Every command emits one JSON object. Check `ok`, preserve `task_id`, and handle a
non-zero exit rather than inferring success.

| Action | Command |
|---|---|
| Preview routing | `codex-worker route --prompt "..." --workdir . --intent read` |
| Start read-only | `codex-worker start --prompt "..." --workdir . --intent read --sandbox read-only --timeout-sec 600` |
| Start scoped write | `codex-worker start --prompt "..." --workdir . --intent write --sandbox workspace-write --allowed-path ./src --timeout-sec 600` |
| Inspect state | `codex-worker status TASK_ID` |
| Wait with a parent deadline | `codex-worker wait TASK_ID --wait-timeout-sec 120` |
| Collect terminal output | `codex-worker result TASK_ID` |
| Request cooperative stop | `codex-worker cancel TASK_ID --wait-timeout-sec 30` |
| Reclaim a verified stuck worker | `codex-worker reap TASK_ID` |

A `wait_timeout` error ends only the controller wait. It does not pause, cancel,
or terminate the worker. Follow it with `status`, then continue waiting or use
task-specific `cancel` and `reap`.

## Mandatory Lifecycle Close-Loop Checklist

Before returning a final answer:

- Account for every dispatched task ID.
- Drive every task to a terminal state: `completed`, `failed`, `timed_out`,
  `cancelled`, `reaped`, or `orphaned`.
- Run `codex-worker result TASK_ID` for every terminal task; inspect the final
  message, exit state, and error even when the task did not complete.
- Review worker-produced diffs and run parent-level validation.
- Cancel workers that are no longer needed.
- Use `reap TASK_ID` only for task-specific recovery. Never kill by executable
  name, broad command substring, repository-wide match, or an unverified PID.
- Do not end with a known task still `queued` or `running`.

This checklist is mandatory even when a manager accepts responsibility, a
local runbook calls workers "fire-and-forget," or cleanup may miss a deadline.

## Red Flags

Stop and close the lifecycle when any of these thoughts appear:

- "The worker is duplicate evidence; someone can inspect it tomorrow."
- "The runbook says fire-and-forget, so I can end the parent task."
- "Posting first and cleaning up later is close enough."
- "The prompt names the directory, so an allowed path is redundant."
- "Try task cancellation briefly, then kill all Codex processes."
- "The wait command timed out, so the worker must have stopped."

None of these changes ownership, scope, or process-identity requirements.

## Complete Read-Only Example

```text
codex-worker start --prompt "Inspect the parser and report likely edge cases. Do not modify files." --workdir . --intent read --sandbox read-only --timeout-sec 600
# Read task_id from the JSON response, then substitute it below.
codex-worker status TASK_ID
codex-worker wait TASK_ID --wait-timeout-sec 120
codex-worker result TASK_ID
```

If `wait` times out, keep the same task ID and continue the close loop with
`status`, `cancel`, and task-specific `reap` as needed. Do not end after the
timeout response.

## Common Mistakes

| Mistake | Correction |
|---|---|
| Discarding the `start` response | Store its `task_id` immediately. |
| Ending after `status` says `completed` | Run `result` and inspect the output. |
| Treating parent wait timeout as worker TTL | Recheck state; explicitly cancel if the worker must stop. |
| Using write intent without allowed paths | Authorize only explicit paths inside the work directory. |
| Running writers on overlapping paths | Divide scopes or serialize them. |
| Using operating-system kill commands | Use `cancel TASK_ID`, then verified `reap TASK_ID`. |
| Trusting a worker's success without review | Inspect its diff and run parent-level validation. |

Read [references/design.md](references/design.md) only when diagnosing persisted
state, recovery behavior, or platform-specific process handling.
