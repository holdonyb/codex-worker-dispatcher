# Contributing

Thanks for helping improve Codex Worker Dispatcher. Keep changes focused,
reviewable, and safe on every supported platform.

## Development setup

Create a Python 3.10+ virtual environment and install the project in editable
mode. The production runtime must remain standard-library-only; test and build
tools may be development dependencies.

```console
python -m venv .venv
./.venv/Scripts/python -m pip install --upgrade pip setuptools wheel
./.venv/Scripts/python -m pip install -e .
```

Use the corresponding `.venv/bin/python` path on macOS and Linux.

## Test-first changes

Use strict test-driven development for every behavior change and bug fix:

1. Write a focused failing test first.
2. Run it and confirm that it fails for the expected reason.
3. Make the smallest implementation change that passes it.
4. Run the focused test and the full suite.

The required Windows workspace validation is:

```console
./.venv/Scripts/python -m unittest discover -s tests -v
```

On macOS and Linux, run the equivalent command with `.venv/bin/python`.
Before submitting, also run `git diff --check`.

## Cross-platform and safety requirements

- Process changes must be safe on Windows, macOS, and Linux. Add deterministic
  unit coverage for platform adapters and native integration coverage where
  the behavior cannot be proved through mocks alone.
- Never add `danger-full-access`. Write routing requires explicit intent and
  one or more allowed paths inside the working directory.
- Verify recorded process identity and ownership before any forceful recovery.
  Never kill by executable name, broad command substring, or repository-wide
  process matching.
- Keep Python 3.10 compatibility.
  Operational commands must write exactly one JSON value to stdout.
  Argparse help remains human-readable text.
- Avoid visible child console windows on Windows; subprocess tests and runtime
  launches must use the project's no-window process helpers.

## Fixtures and public data

Do not use real prompts, credentials, Codex configuration, model output, or
worker state in fixtures. In particular, do not commit runtime task files or
copy a real task directory into the repository. Build synthetic in-memory or
temporary-directory fixtures with obviously fictitious content, and ensure
tests clean them up.

## Pull requests

Explain the behavior change, the RED-to-GREEN evidence, and platform-specific
risks. Include the focused and full-suite commands you ran. Keep unrelated
formatting and refactors out of the change. Report security issues through the
private process in [SECURITY.md](SECURITY.md), not through a pull request or
public issue.
