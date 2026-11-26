# How it was built

The way of working behind the emulator: a light V-model and an AI assisted workflow, so
every line of code exists because a requirement asks for it, and every requirement is
checked.

## The light V-model

Each step of defining the system (left) has a matching check (right). "Light" means no
heavy paperwork: requirements are one page, checks are the tests plus a table of manual
checks, and each test's docstring (`"""Checks REQ-04."""`) links the two. The full table is
in [Verification](verification.md#how-requirements-are-checked).

```
Idea ............................................ Runs on the Pi (v1.0)
  Requirements ................................ Each REQ has a test or manual check
    High level: engine.py ................... test_engine.py
      Mid level: pack.py .................. test_pack.py
        Low level: cell.py .............. test_cell.py
                        \              /
                           code
```

## The AI assisted workflow

An AI coding assistant writes most of the code; a human owns the project and reviews
everything. The rules the assistant follows are in `AGENTS.md`.

| Step | What happens |
|---|---|
| 1. Requirements first | every feature starts as a requirement on the [Requirements](requirements.md) page, agreed before any code. Only what a requirement asks for is built |
| 2. Explain, then draft | the AI explains what it will change and why, then drafts code and tests on a branch, one task per branch, never on `main` |
| 3. Test every change | each test names its requirement. `uv run pytest`, `uv run ruff check .` and `uv run ruff format --check .` pass before a task is done; CI runs them on every push and pull request |
| 4. Review every change | a human reads and understands each change before merging the pull request. CI must be green |
| 5. Record hardware checks | what needs real hardware (booting a Pi, unplugging an adapter) is checked by hand and recorded in the [manual checks](verification.md#manual-checks) |
