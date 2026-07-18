# Agent Guidelines

## Project Contract

- Support Python 3.10 and newer.
- Keep the runtime standard-library-only.
- Write tests with `unittest`.
- Reserve stdout for machine-readable JSON during operational commands;
  argparse help remains human-readable.
- Never run Codex workers with `danger-full-access`.
- Follow strict test-driven development: write and observe a failing test before implementation.

## Validation

On this Windows workspace, run:

```powershell
./.venv/Scripts/python -m unittest discover -s tests -v
```

CI covers macOS and Linux compatibility.
