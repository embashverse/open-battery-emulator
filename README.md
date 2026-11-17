# Open Battery Emulator

A battery pack in software for the Raspberry Pi. Current goes in, the pack reacts like a
real battery, and BMS style data goes out on CAN. Signals only, no real power.

> Early development. The emulator runs in the terminal with simulated current; CAN comes
> with milestone M3.

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

## Run it

```
uv sync
uv run python -m emulator.engine
```

Options: `--config other.yaml` (default `config.yaml`) and `--speed 10` (run 10 times
faster than real time; each step is still 100 ms of emulated time).

It prints one line per second of emulated time:

```
t=     1.0 s  DISCHARGING  I=   50.0 A  V=  315.36 V  SOC= 80.00 %  min 3.283 V (M7 C5)  max 3.287 V (M16 C6)
```

- `t` emulated time since start
- state: IDLE, CHARGING, DISCHARGING, FULL or EMPTY
- `I` pack current that really flowed, discharge positive (0 inside the deadband or
  when FULL/EMPTY blocks it)
- `V` pack voltage
- `SOC` average SOC of all cells
- `min` / `max` lowest and highest cell voltage, with module (`M`) and cell (`C`) number

Ctrl+C stops it with a one line summary. A bad setting in `config.yaml` stops it at
start with a message naming the setting.

## Quick start

Coming with milestone M3. Needs Linux, Python 3.11+ and [uv](https://docs.astral.sh/uv/).
No CAN hardware needed for development, it runs on virtual CAN (`vcan0`).
