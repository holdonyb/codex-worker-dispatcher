# Project Status

- Approved design: `docs/superpowers/specs/2026-07-17-cross-platform-codex-worker-dispatcher-design.md`
- Implementation plan: `docs/superpowers/plans/2026-07-17-cross-platform-codex-worker-dispatcher.md`
- Current phase: Task 10 — release candidate verified locally; sanitized snapshot publication and hosted CI pending
- Completed: package contract, routing policy, atomic state, verified process termination, lifecycle CLI, Agent Skill, installer, public documentation, release hygiene, and cross-platform CI workflow
- Local validation (Windows, 2026-07-18): 296 tests passed with 5 platform skips; the formerly intermittent CLI lifecycle passed 30 consecutive black-box runs; sdist/wheel build, offline clean-venv wheel install, version JSON, and temporary-target Skill install passed
- Release gate: the final integrated public audit must pass, the sanitized public snapshot must be published, and every Windows, Ubuntu, and macOS GitHub Actions matrix job must pass before release `v0.1.0`
- Publication history gate: create the public default branch from a sanitized snapshot; do not push the pre-audit development history
- Snapshot method: export the verified committed tracked tree into a temporary directory, initialize a new `main` repository with one release commit, and rerun the public audit, tests, and build there before pushing
- Security gate: enable and verify GitHub private vulnerability reporting before the first public snapshot push
- Validation: `./.venv/Scripts/python -m unittest discover -s tests -v`
- Public target: `holdonyb/codex-worker-dispatcher`
- Remote repository: Not created yet
