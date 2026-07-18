# Codex Skill-First Distribution Design

**Date:** 2026-07-18

**Status:** Approved for implementation

**Repository:** `holdonyb/codex-worker-dispatcher`

**Target release:** `v0.1.1`

## 1. Goal

Keep the existing cross-platform runtime and public repository, but correct the
distribution shape so the repository is a native Codex Skill first and a Python
CLI package second.

After this change:

- the repository root is the canonical `dispatching-codex-workers` Skill;
- local clones and GitHub snapshots are directly discoverable as a Codex Skill;
- `codex-worker skill install` copies that same root Skill into
  `$CODEX_HOME/skills/dispatching-codex-workers`, or
  `~/.codex/skills/dispatching-codex-workers` when `CODEX_HOME` is unset;
- the packaged wheel and sdist ship the same Skill files as the source tree;
- README and release documentation present the project as a Codex Skill with a
  bundled runtime, not as a runtime with a nested optional Skill.

The runtime behavior, lifecycle contract, platform support, and safety
guarantees from `v0.1.0` remain unchanged unless a change is required to make
Skill-first distribution consistent.

## 2. Problem Statement

`v0.1.0` shipped a valid Skill body, but the repository shape is not Codex
native:

- the canonical Skill lives under `skill/dispatching-codex-workers/` instead of
  the repository root;
- the installer defaults to `~/.agents/skills`, which does not match the Codex
  Skill discovery contract;
- the README leads with CLI installation and treats the Skill as a bundled
  secondary artifact;
- a user who clones the repository into a Skill directory does not get the
  expected root `SKILL.md` experience.

This creates friction for the exact public sharing goal: “share this as a Codex
Skill”.

## 3. Selected Approach

### A. Make the repository root the canonical Skill and keep the runtime in the same repository

This is the selected approach.

The root of the repository will contain:

- `SKILL.md`
- `agents/openai.yaml`
- `references/design.md`

The Python runtime, tests, docs, CI, and packaging stay in the same repository.
This preserves the validated implementation and release pipeline while fixing
how Codex discovers and installs the Skill.

### B. Keep the nested Skill and add more installer/documentation glue

This is rejected. It preserves the mismatch between “public Skill repo” and
actual repo shape, and it forces every installation path to compensate for a
non-native layout.

### C. Split into two public repositories

This is rejected for `v0.1.1`. It adds release coordination, version drift
risk, and a second maintenance surface without solving a real runtime problem.

## 4. Repository Contract After The Change

The repository root becomes a Codex-native Skill folder that also contains the
runtime project:

```text
codex-worker-dispatcher/
├── SKILL.md
├── agents/openai.yaml
├── references/design.md
├── src/codex_worker_dispatcher/
├── tests/
├── docs/
├── .github/workflows/ci.yml
├── README.md
├── README.zh-CN.md
├── PROJECT_STATUS.md
└── pyproject.toml
```

The nested `skill/dispatching-codex-workers/` tree is removed. There is one
canonical Skill source only.

## 5. Installation Model

There are two supported installation paths, and both must install the same
Skill content.

### Direct Skill use from a clone

If a user clones the repository into
`$CODEX_HOME/skills/dispatching-codex-workers` or
`~/.codex/skills/dispatching-codex-workers`, Codex can discover it directly
from the root `SKILL.md`.

This path gives the user the Skill instructions immediately, but the runtime
CLI is still a separate prerequisite. The README must say this plainly.

### Owned install through the bundled CLI

If a user installs the package with `pipx` or a virtual environment, then
`codex-worker skill install` copies the root Skill files into the default Codex
Skill directory and writes the existing ownership marker.

Default target resolution:

1. if `CODEX_HOME` is set, install to
   `$CODEX_HOME/skills/dispatching-codex-workers`;
2. otherwise install to
   `~/.codex/skills/dispatching-codex-workers`.

`--target` remains available for explicit overrides.

## 6. Packaging Contract

The source tree and built artifacts must resolve the bundled Skill from the same
logical structure.

Source-tree lookup:

- prefer the installed data-files location when running from a wheel;
- otherwise resolve the bundled Skill from the repository root.

Setuptools data-files:

- ship root `SKILL.md`;
- ship `agents/openai.yaml`;
- ship `references/design.md`.

The installed package layout under `share/codex-worker-dispatcher/skill/` may
remain the same, but the input files come from the repository root rather than
from a nested duplicate tree.

## 7. Documentation Contract

README must lead with the project as a Codex Skill:

- what the Skill does;
- why the runtime exists;
- that the runtime is required for actual worker dispatch;
- direct clone placement for Skill discovery;
- `pipx install ...` plus `codex-worker skill install` as the managed path.

The default Skill location in docs must be updated everywhere from
`~/.agents/skills` to `$CODEX_HOME/skills` or `~/.codex/skills`.

The invocation examples must continue to use `$dispatching-codex-workers` and
must not imply that cloning alone installs the `codex-worker` executable.

## 8. Testing Changes

Implementation follows TDD and must add or update failing tests before code.

Required coverage:

- root Skill presence and required files;
- nested Skill removal;
- installer default target uses `CODEX_HOME` when set;
- installer fallback target is `~/.codex/skills/...`;
- packaged data-files point at root Skill assets;
- public-release docs reference Codex Skill paths and Skill-first wording.

Existing lifecycle, runtime, and cross-platform tests remain in place and must
continue to pass unchanged.

## 9. Release And Publication

This is a corrective public release, not a history rewrite.

- bump project version to `0.1.1`;
- keep `v0.1.0` intact;
- publish a new sanitized public commit sequence to the existing public repo;
- require the same local validation and hosted multi-platform CI before the new
  release is tagged.

The public repository remains `PUBLIC`.

## 10. Non-Goals

`v0.1.1` does not include:

- a runtime rewrite;
- PyPI publication;
- a separate Skill-only repository;
- stronger sandboxing than the existing runtime contract;
- behavior changes to task lifecycle, routing, or recovery beyond what the
  Skill-first distribution fix requires.
