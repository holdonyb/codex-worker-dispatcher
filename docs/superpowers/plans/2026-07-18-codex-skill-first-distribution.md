# Codex Skill-First Distribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert `codex-worker-dispatcher` into a root-level Codex Skill repository while preserving the validated cross-platform runtime and release pipeline.

**Architecture:** Keep one canonical Skill source at the repository root and make both source installs and packaged installs copy that same Skill tree. Limit code changes to installer resolution, package metadata, docs, and tests so the runtime lifecycle behavior stays unchanged.

**Tech Stack:** Python 3.10+, `unittest`, setuptools data-files, GitHub Actions

---

### Task 1: Red tests for root Skill layout and installer defaults

**Files:**
- Modify: `tests/test_skill.py`
- Modify: `tests/test_installer.py`
- Test: `tests/test_skill.py`
- Test: `tests/test_installer.py`

- [ ] **Step 1: Write the failing test**

```python
ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT
SKILL_PATH = ROOT / "SKILL.md"
DESIGN_PATH = ROOT / "references" / "design.md"
OPENAI_PATH = ROOT / "agents" / "openai.yaml"

def test_root_skill_contract(self) -> None:
    self.assertTrue((ROOT / "SKILL.md").is_file())
    self.assertFalse((ROOT / "skill" / "dispatching-codex-workers").exists())

def test_default_install_prefers_codex_home_then_dot_codex(self) -> None:
    with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
        self.assertEqual(
            default_skill_target(),
            codex_home / "skills" / "dispatching-codex-workers",
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python -m unittest tests.test_skill tests.test_installer -v`
Expected: FAIL because the repo still points at `skill/dispatching-codex-workers` and `.agents/skills`.

- [ ] **Step 3: Write minimal implementation**

```python
def default_skill_target() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return base / "skills" / _SKILL_NAME
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python -m unittest tests.test_skill tests.test_installer -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_skill.py tests/test_installer.py src/codex_worker_dispatcher/installer.py
git commit -m "test: enforce root skill layout defaults"
```

### Task 2: Red tests for packaged Skill source and repository metadata

**Files:**
- Modify: `tests/test_package.py`
- Modify: `tests/test_public_release.py`
- Modify: `pyproject.toml`
- Modify: `src/codex_worker_dispatcher/__init__.py`
- Test: `tests/test_package.py`
- Test: `tests/test_public_release.py`

- [ ] **Step 1: Write the failing test**

```python
for data_file_mapping in (
    '"share/codex-worker-dispatcher/skill/dispatching-codex-workers" = ["SKILL.md"]',
    '"share/codex-worker-dispatcher/skill/dispatching-codex-workers/agents" = ["agents/openai.yaml"]',
    '"share/codex-worker-dispatcher/skill/dispatching-codex-workers/references" = ["references/design.md"]',
):
    self.assertIn(data_file_mapping, pyproject)

self.assertEqual(__version__, "0.1.1")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python -m unittest tests.test_package tests.test_public_release -v`
Expected: FAIL because the package still maps nested Skill files and the version is `0.1.0`.

- [ ] **Step 3: Write minimal implementation**

```toml
[tool.setuptools.data-files]
"share/codex-worker-dispatcher/skill/dispatching-codex-workers" = ["SKILL.md"]
"share/codex-worker-dispatcher/skill/dispatching-codex-workers/agents" = ["agents/openai.yaml"]
"share/codex-worker-dispatcher/skill/dispatching-codex-workers/references" = ["references/design.md"]
```

```python
__version__ = "0.1.1"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python -m unittest tests.test_package tests.test_public_release -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_package.py tests/test_public_release.py pyproject.toml src/codex_worker_dispatcher/__init__.py
git commit -m "build: package root skill assets"
```

### Task 3: Move the canonical Skill to the repository root

**Files:**
- Create: `SKILL.md`
- Create: `agents/openai.yaml`
- Create: `references/design.md`
- Delete: `skill/dispatching-codex-workers/SKILL.md`
- Delete: `skill/dispatching-codex-workers/agents/openai.yaml`
- Delete: `skill/dispatching-codex-workers/references/design.md`
- Modify: `src/codex_worker_dispatcher/installer.py`
- Test: `tests/test_skill.py`
- Test: `tests/test_installer.py`

- [ ] **Step 1: Write the failing test**

```python
def test_root_skill_reference_contains_deferred_runtime_details(self) -> None:
    skill = self._read_required(ROOT / "SKILL.md")
    design = self._read_required(ROOT / "references" / "design.md")
    self.assertIn("references/design.md", skill)
    for required_term in ("manifest", "Windows", "Linux", "macOS"):
        self.assertIn(required_term, design)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python -m unittest tests.test_skill -v`
Expected: FAIL because the root Skill files do not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
def _bundled_skill_source() -> Path:
    data_root = sysconfig.get_path("data")
    candidates: list[Path] = []
    if data_root:
        candidates.append(
            Path(data_root) / "share" / "codex-worker-dispatcher" / "skill" / _SKILL_NAME
        )
    candidates.append(Path(__file__).resolve().parents[2])
```

Move the current Skill content unchanged to:

```text
SKILL.md
agents/openai.yaml
references/design.md
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python -m unittest tests.test_skill tests.test_installer -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add SKILL.md agents/openai.yaml references/design.md skill/dispatching-codex-workers src/codex_worker_dispatcher/installer.py tests/test_skill.py tests/test_installer.py
git commit -m "feat: make repository root the canonical skill"
```

### Task 4: Rewrite docs and status for Skill-first distribution

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `PROJECT_STATUS.md`
- Test: `tests/test_public_release.py`

- [ ] **Step 1: Write the failing test**

```python
for snippet in (
    "$CODEX_HOME/skills/dispatching-codex-workers",
    "~/.codex/skills/dispatching-codex-workers",
    "Codex Skill",
):
    self.assertIn(snippet, text)

self.assertNotIn("~/.agents/skills/dispatching-codex-workers", text)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python -m unittest tests.test_public_release -v`
Expected: FAIL because the README and status docs still describe the old install path and hybrid framing.

- [ ] **Step 3: Write minimal implementation**

```markdown
Clone or install the repository as a Codex Skill, then install the bundled runtime:

git clone https://github.com/holdonyb/codex-worker-dispatcher.git "${CODEX_HOME:-$HOME/.codex}/skills/dispatching-codex-workers"
pipx install git+https://github.com/holdonyb/codex-worker-dispatcher.git
codex-worker skill install
```

Update `PROJECT_STATUS.md` to mark `v0.1.1` as the Skill-first correction release target.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/Scripts/python -m unittest tests.test_public_release -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add README.md README.zh-CN.md PROJECT_STATUS.md tests/test_public_release.py
git commit -m "docs: present skill-first installation"
```

### Task 5: Validate, build, and prepare the public release

**Files:**
- Modify: `PROJECT_STATUS.md`
- Test: `tests/test_skill.py`
- Test: `tests/test_installer.py`
- Test: `tests/test_package.py`
- Test: `tests/test_public_release.py`

- [ ] **Step 1: Run focused validation**

Run: `./.venv/Scripts/python -m unittest tests.test_skill tests.test_installer tests.test_package tests.test_public_release -v`
Expected: PASS

- [ ] **Step 2: Run full validation**

Run: `./.venv/Scripts/python -m unittest discover -s tests -v`
Expected: PASS with only expected platform skips

- [ ] **Step 3: Validate the root Skill**

Run: `python E:/.codex/skills/.system/skill-creator/scripts/quick_validate.py E:/work/codex-worker-dispatcher/.worktrees/feature-cross-platform`
Expected: PASS

- [ ] **Step 4: Build artifacts**

Run: `./.venv/Scripts/python -m build`
Expected: sdist and wheel created successfully

- [ ] **Step 5: Commit**

```bash
git add PROJECT_STATUS.md
git commit -m "docs: record v0.1.1 skill-first validation"
```
