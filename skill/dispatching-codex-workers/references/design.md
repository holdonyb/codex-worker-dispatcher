# Runtime Design Reference

Use this reference when diagnosing worker state, reconciliation, cancellation,
or verified process recovery. Use `codex-worker --help` for exhaustive CLI
flags.

## Persisted Task State

The state root defaults to `$CODEX_HOME/worker-runs`; when `CODEX_HOME` is not
set, it defaults under the current user's `.codex` directory. Each private task
directory can contain:

- `manifest.json`: authoritative lifecycle state
- `prompt.txt`: submitted prompt
- `events.jsonl`: Codex event stream
- `stderr.log`: worker diagnostics
- `last-message.txt`: final Codex response
- `cancel.request`: cooperative cancellation marker, when requested
- `.manifest.lock`: cross-process manifest update lock

Manifest schema version 2 records the task ID, status, creation/update/start/end
timestamps, work directory, resolved route, allowed paths, TTL, engine, process
identities, ownership-nonce hash, runner launch reservation, exit code, and
error. The ownership nonce itself stays in owned supervisor and runner command
lines so process identity can be checked without persisting that secret in the
manifest.

Non-terminal state is `queued` or `running`. Terminal states are `completed`,
`failed`, `timed_out`, `cancelled`, `reaped`, and `orphaned`. Terminal updates
are first-wins. Manifest changes use a cross-process lock plus an atomic replace
so controllers and the supervisor do not overwrite one another's terminal
decision.

`status`, `wait`, and `result` reconcile inactive non-terminal tasks. A complete
final message plus completion event can reconcile to `completed`; missing
completion evidence reconciles to `orphaned`. A controller `wait_timeout` does
not change worker state.

## Process Ownership and Recovery

The supervisor is detached from the launching shell. The runner starts in a
dedicated process group or tree and carries the task directory plus a random
ownership nonce. Before termination, the runtime verifies the recorded PID,
process start identity, exact worker role, task directory, and nonce. A failed
identity check refuses termination rather than risking an unrelated process.

### Windows

The adapter queries process creation time and command line through the built-in
Windows CIM interface, then uses verified tree termination. PowerShell 7 is not
required.

### Linux

The adapter reads process identity from `/proc`, verifies the dedicated process
group, and anchors group members with pidfds for safe signaling. It applies a
bounded graceful termination before force only while ownership remains proven.

### macOS

The adapter reads start identity and command line with `ps`, verifies the
dedicated POSIX process group, and uses a bounded graceful-then-force sequence
with repeated identity checks.

No platform adapter kills by executable image name, broad substring, or
repository-wide match. `reap` reports an identity or state error when ownership
cannot be established safely.
