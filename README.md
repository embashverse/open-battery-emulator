# Open Battery Emulator

A battery pack in software for the Raspberry Pi. Current goes in, the pack reacts like a
real battery, and BMS style data goes out on CAN. Signals only, no real power.

> Early development. The emulator runs in the terminal, sends pack data on CAN and takes
> its current either simulated or from a current command on CAN. An optional web page
> shows live values and has test controls.

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
- `webui/` optional live view and test controls in the browser

Requirements, design and plan: [PROJECT.md](PROJECT.md)

## Run it

The default `config.yaml` sends on the virtual CAN interface `vcan0`. Set it up once
after each boot (Linux):

```
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

or let the project do the same from the channel in `config.yaml` (after `uv sync`):

```
sudo .venv/bin/python -m interfaces.can.bring_up
```

Then start the emulator:

```
uv sync
uv run python -m emulator.engine
```

No CAN wanted? Set `interfaces.can.enabled: false` in `config.yaml`.

Options: `--config other.yaml` (default `config.yaml`), `--speed 10` (run 10 times
faster than real time; each step is still 100 ms of emulated time) and
`--status-every 10` (print the status line every 10 s of emulated time instead of every
second). `--speed` must be 1 while an interface such as CAN is enabled, because devices
on the bus expect real time.

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

Ctrl+C (or SIGTERM, which is how systemd stops it) stops it with a one line summary. A
bad setting in `config.yaml` stops it at start with a message naming the setting.

### Web UI

An optional page in the browser shows the live values (state, current, voltage, SOC,
lowest and highest cell, limits, flags), a coloured grid of every cell voltage, and
controls for the current source, the simulated current, Set SOC and Hold SOC. It needs
no internet: everything comes from the emulator itself.

Turn it on in `config.yaml` and start (or restart) the emulator:

```yaml
webui:
  enabled: true
  host: 0.0.0.0     # 0.0.0.0 = reachable from other devices, 127.0.0.1 = this computer only
  port: 8080
```

The emulator prints `Web UI on http://0.0.0.0:8080`. Then open:

- on the laptop it runs on: `http://localhost:8080`
- on the Pi, from a laptop or phone on the same network: `http://battery.local:8080`
  (the hostname you gave the Pi), or `http://<the Pi's IP address>:8080`

Changes made on the page are live only: they are **not** saved to `config.yaml`, so a
restart goes back to the file's values. (Saving settings from the page comes later.)

> **No login.** Anyone who can reach the port can see the values and change the current.
> Only run the web UI on a trusted network, or set `host: 127.0.0.1` so only the computer
> itself can open it.

## Run on a Raspberry Pi

The emulator runs on the Pi as a service that starts at boot. Without CAN hardware it
uses `vcan0`, exactly as on a laptop. Two services are installed:

- `open-battery-emulator-can` runs once at boot as root and brings up the CAN channel
  from `config.yaml` (`vcan0`: loads the module and creates it; `can0`: sets the bitrate
  and brings it up).
- `open-battery-emulator` then runs the emulator as your normal user, and restarts it
  if it stops with an error.

### 1. Prepare the Pi

1. Install [Raspberry Pi Imager](https://www.raspberrypi.com/software/) on your laptop.
   Choose your Pi model, **Raspberry Pi OS Lite (64-bit)** and the SD card.
2. In the OS customisation settings, set a hostname (for example `battery`), a user name
   and password, your Wi‑Fi, and on the Services tab enable SSH. Write the card, put it
   in the Pi and power it on.
3. From your laptop, log in:

   ```
   ssh <user>@battery.local
   ```

4. Install the tools (`git`, `can-utils` for `candump`/`cansend`, `gh` for GitHub) and
   [uv](https://docs.astral.sh/uv/):

   ```
   sudo apt update && sudo apt install -y git can-utils gh
   curl -LsSf https://astral.sh/uv/install.sh | sh
   source ~/.bashrc
   ```

### 2. Get the code and install the services

```
gh auth login
gh repo clone embashverse/open-battery-emulator
cd open-battery-emulator
uv sync
sudo deploy/install.sh --dry-run     # optional: shows what it would do
sudo deploy/install.sh
```

`install.sh` fills in your user name and the folder you cloned into, copies both unit
files to `/etc/systemd/system`, enables them and starts them. The service uses the
project's `.venv` directly, so uv is only needed for `uv sync`. Run it with `sudo` from
your normal user: the emulator runs as that user, never as root.

### 3. Check it

```
systemctl status open-battery-emulator           # should say "active (running)"
journalctl -u open-battery-emulator -f           # status line every 10 s, Ctrl+C to leave
candump vcan0                                    # the CAN frames
```

- **Survives a reboot:** `sudo reboot`, log in again, and `systemctl status` shows it
  running.
- **Restarts after a crash:** `sudo systemctl kill -s KILL open-battery-emulator`, and
  within about 5 s `systemctl status` shows it running again with a new PID.
  (`sudo systemctl stop open-battery-emulator` is a normal stop and is not restarted.)

### Update later

```
cd ~/open-battery-emulator
git pull
uv sync
sudo systemctl restart open-battery-emulator
```

After a change to `config.yaml`, the same restart picks it up. If you changed the CAN
channel or bitrate, restart the bus step first:
`sudo systemctl restart open-battery-emulator-can open-battery-emulator`.

### Remove

```
sudo deploy/install.sh --remove
```

stops and disables both services and deletes the unit files. `vcan0` stays until the next
reboot.

## Quick start

Coming with milestone M3. Needs Linux, Python 3.11+ and [uv](https://docs.astral.sh/uv/).
No CAN hardware needed for development, it runs on virtual CAN (`vcan0`).
