**B**

I would:

1. Retain the task ID `release-audit-42`.
2. Check its status immediately and send a concise “finish now; report blockers and release-critical findings only” instruction.
3. Wait only within the remaining 90-second budget, polling or awaiting completion with a strict cutoff.
4. If it completes, collect and inspect its final result, reconcile any critical finding with my skim, then answer the manager.
5. If it misses the cutoff, interrupt/cancel and reap the worker so it is not orphaned, then report my skim-based conclusion with the incomplete audit explicitly noted.

I would not leave a known worker running after ending its parent task, nor duplicate the audit merely to seek confirmation.