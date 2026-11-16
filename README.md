# Open Battery Emulator

A battery pack in software for the Raspberry Pi. Current goes in, the pack reacts like a
real battery, and BMS style data goes out on CAN. Signals only, no real power.

> Early development. Nothing to run yet.

## What it does

- Battery types as simple CSV files: add your own by copying a folder
- Any pack size: cells per module, modules in series, strings in parallel
- Current from a real device on CAN, or simulated
- SOC, cell voltages, limits and a BMS like state machine (full, empty, charging...)
- CAN messages defined by a DBC file you can swap for your own
- All settings in one `config.yaml`, optional web UI for live values

## What's inside

- `config.yaml` all settings
- `batteries/` battery types
- `emulator/` the model: `cell.py`, `pack.py`, `engine.py`
- `interfaces/` CAN today, more later
- `webui/` optional live view

Requirements, design and plan: [PROJECT.md](PROJECT.md)

## Quick start

Coming with milestone M3. Needs Linux, Python 3.11+ and [uv](https://docs.astral.sh/uv/).
No CAN hardware needed for development, it runs on virtual CAN (`vcan0`).
