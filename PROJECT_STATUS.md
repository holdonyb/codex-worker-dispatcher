# Project Status

- Approved design: `docs/superpowers/specs/2026-07-17-cross-platform-codex-worker-dispatcher-design.md`
- Implementation plan: `docs/superpowers/plans/2026-07-17-cross-platform-codex-worker-dispatcher.md`
- Skill-first correction design: `docs/superpowers/specs/2026-07-18-codex-skill-first-distribution-design.md`
- Skill-first correction plan: `docs/superpowers/plans/2026-07-18-codex-skill-first-distribution.md`
- Current phase: `v0.1.1` Skill-first distribution correction in progress
- Completed in `v0.1.0`: package contract, routing policy, atomic state, verified process termination, lifecycle CLI, Agent Skill, installer, public documentation, release hygiene, sanitized publication, and hosted cross-platform validation
- Pending for `v0.1.1`: root-level Codex Skill layout, default install path switch to `$CODEX_HOME/skills` or `~/.codex/skills`, packaging remap, README reframing, and public re-release
- Local validation (Windows, 2026-07-18): 304 tests passed with 6 platform skips; sdist/wheel build, targeted public-release audit, version JSON, temporary-target Skill install, root Skill validation, and source/installed Skill validation passed
- Hosted validation: all six Windows, Ubuntu, and macOS jobs for Python 3.10 and 3.14 passed in [GitHub Actions run 29630973235](https://github.com/holdonyb/codex-worker-dispatcher/actions/runs/29630973235)
- Publication provenance: public `main` began from a locally verified, single-commit sanitized snapshot; the pre-audit development history was not pushed
- Security: GitHub private vulnerability reporting is enabled
- Release: [v0.1.0](https://github.com/holdonyb/codex-worker-dispatcher/releases/tag/v0.1.0)
- Validation: `./.venv/Scripts/python -m unittest discover -s tests -v`
- Public target: `holdonyb/codex-worker-dispatcher`
- Remote repository: https://github.com/holdonyb/codex-worker-dispatcher
