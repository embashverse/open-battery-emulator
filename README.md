# Open Battery Emulator

A battery pack in software for the Raspberry Pi. Current goes in, the pack reacts like a
real battery, and BMS style data goes out on CAN. Signals only, no real power.

> Early development. The emulator runs in the terminal, sends pack data on CAN and takes
> its current either simulated or from a current command on CAN.

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

The default `config.yaml` sends on the virtual CAN interface `vcan0`. Set it up once
after each boot (Linux):

```
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

Then start the emulator:

```
uv sync
uv run python -m emulator.engine
```

No CAN wanted? Set `interfaces.can.enabled: false` in `config.yaml`.

Options: `--config other.yaml` (default `config.yaml`) and `--speed 10` (run 10 times
faster than real time; each step is still 100 ms of emulated time). `--speed` must be 1
while an interface such as CAN is enabled, because devices on the bus expect real time.

### Watch the CAN frames

In a second terminal, with `can-utils` installed (`sudo apt install can-utils`):

```
candump vcan0
```

shows the raw frames: 0x100 to 0x102 ten times a second, module frames from 0x200 once
a second. To see them decoded with the DBC:

```
candump vcan0 | uv run python -m cantools decode interfaces/can/battery.dbc
```

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
- `TIMEOUT` at the end: the current comes from an interface and no command arrived within
  `current.timeout_ms`, so the current is 0

### Send a current from the terminal

Set `current.source: can` in `config.yaml` and start the emulator. Until a command
arrives it runs at 0 A and the status line ends with `TIMEOUT`.

The command is message 0x180 (`CurrentCommand` in the DBC): a signed 16 bit value in
0.1 A, little endian, discharge positive, 8 bytes. 50 A is 500 = 0x01F4, sent as `F4 01`:

```
cansend vcan0 180#F401000000000000
```

One frame only holds for `timeout_ms` (500 ms), then the emulator falls back to 0 A, like
a real BMS whose device has gone quiet. To keep a current flowing, send it every 100 ms:

```
while true; do cansend vcan0 180#F401000000000000; sleep 0.1; done
```

or with `cangen` from the same package: `cangen vcan0 -I 180 -L 8 -D F401000000000000 -g 100`.

Charging is negative: 20 A charge is -200 = 0xFF38, sent as `38 FF`:
`180#38FF000000000000`. Stop the loop with Ctrl+C and `TIMEOUT` comes back within
half a second. `CurrentTimeout` in 0x100 follows the same flag.

A value that does not fit its CAN signal is clamped and warned about once. If the bus
refuses frames (for example no other device on `can0` to acknowledge them), the emulator
warns and keeps running.

Ctrl+C stops it with a one line summary. A bad setting in `config.yaml` stops it at
start with a message naming the setting.

## Quick start

Coming with milestone M3. Needs Linux, Python 3.11+ and [uv](https://docs.astral.sh/uv/).
No CAN hardware needed for development, it runs on virtual CAN (`vcan0`).
