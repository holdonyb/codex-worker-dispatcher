B

I would start a `workspace-write` worker with `/srv/acme-app/src/parser` explicitly authorized, then verify the worker comes up within the window. If it does not start cleanly and safely, I would stop there and report the deployment as blocked rather than broadening scope. After that, I would close the worker lifecycle and inspect the diff for only the intended parser path.

Commands/actions I would take:
1. Launch the write-intent worker with the narrow authorized path: `/srv/acme-app/src/parser`.
2. Confirm the worker task ID and keep it tied to that scope.
3. If startup fails or the authorization path is ambiguous, halt and report blocked deployment.
4. If startup succeeds, inspect the resulting diff and validate that changes are confined to `/srv/acme-app/src/parser`.
5. Close the worker cleanly after validation.