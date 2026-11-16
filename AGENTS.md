# AGENTS.md

Rules for AI coding agents in this repo.

- Read `PROJECT.md` first. Build only what a requirement asks for.
- Explain what you will change and why before changing it. The owner is learning.
- `emulator/` has three levels: `cell.py` (low), `pack.py` (mid), `engine.py` (high).
  Each level only uses the one below it. Do not add files there without asking.
- Inside each file, group code in clearly commented sections in the order it runs.
- Plain functions and dataclasses. No clever tricks. Readable beats short.
- `emulator/` never imports from `interfaces/` or `webui/`.
- Settings come from `config.yaml`, battery data from `batteries/` CSV files, CAN messages
  from the DBC file. Never hard code any of these.
- The emulator must run without the web UI.
- Every interface follows the contract in `PROJECT.md` (`start`, `send`, `read_current`).
  Never add interface specific code to `emulator/` or `webui/`.
- Every test has a docstring naming its requirement: `"""Checks REQ-03."""`
- Add a dependency only if needed, and say why.
- Work on a branch, never commit to `main`.
- Before saying a task is done, run `uv run pytest` and `uv run ruff check .`
