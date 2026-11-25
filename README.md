# Open Battery Emulator

A battery pack in software for the Raspberry Pi. Current goes in, the pack reacts like a
real battery, and BMS style data goes out on CAN. Signals only, no real power.

> Early development. The emulator runs in the terminal, sends pack data on CAN and takes
> its current either simulated or from a current command on CAN. An optional web page
> shows live values, has test controls, a Play / Stop button and a Config tab to edit
> `config.yaml`.

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
- `webui/` optional live view, Play / Stop and settings in the browser

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

An optional page in the browser with two tabs. Errors and confirmations appear next to
the button that caused them: under the run button (Live) or under Save (Config). It needs
no internet: everything comes from the emulator itself.

- **Live:** SOC with the time to empty or full, voltage, current, state, the lowest and
  highest cell, the spread, limits and flags (hover or tap a symbol or flag for an
  explanation), and a grid of every cell with the strings side by side (green = normal,
  amber = far from the pack average, red = at its minimum or maximum voltage or the cell
  that caused FULL or EMPTY; hover or tap for its voltage and SOC). On the right: controls
  for the current source, the simulated current (Discharge or Charge and the amps), Set
  SOC and Hold SOC, the run time, and one run button that shows whether the emulator is
  running and says what pressing it does (Play or Stop).
- **Config:** every setting in `config.yaml`, grouped: battery (a list of the folders in
  `batteries/`), pack (with a summary like `96s2p · 316 V nominal · 200 Ah`), start,
  cell variation, current, interfaces and web UI. Every section under `interfaces:`
  shows up here on its own. The Save card shows how many changes are not saved yet.

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

Changes made with the Live controls are live only: they are **not** saved to
`config.yaml`, so the next Play goes back to the file's values.

#### Play and Stop

The emulator is either **RUNNING** (green: ticking, sending on CAN) or **STOPPED** (grey:
nothing ticks, the interfaces are closed, so no CAN frames). The web server keeps running
in both, so the page always works.

- **Stop** closes the interfaces. The last values stay on the Live tab.
- **Play** reads the saved `config.yaml`, builds a fresh pack and opens the interfaces.
  Time and the live controls start over. If something is wrong (a bad setting, a missing
  CAN channel...), it stays STOPPED and the page shows the error under the run button.

`start.autostart: true` (the default) presses Play at start, so the Pi service is on the
bus straight after boot. With `autostart: false` it starts STOPPED and waits for Play.
If Play fails at start and the web UI is on, it also waits STOPPED, so you can fix the
setting from the browser. Ctrl+C and SIGTERM still stop everything.

#### Editing settings

1. Press **Stop**: settings can only be edited while STOPPED.
2. Change values in the **Config** tab and press **Save**. The emulator checks them with
   the same checks it runs at Play and shows the same error message if one is wrong; then
   the file is left untouched. A good Save writes `config.yaml`, keeping its comments.
3. Press **Play** to run with the new settings.

Interface settings (like the DBC file) are checked by the interface itself, so their
errors show up at Play, not at Save.

If `config.yaml` changes on disk while the page is open (for example you edited it over
SSH), the page shows a notice with a **Reload** button under Save, and refuses to Save over the
newer file.

`webui.host` and `webui.port` are saved like everything else, but only take effect after
restarting the program (`sudo systemctl restart open-battery-emulator` on the Pi).

> **No login.** Anyone who can reach the port can see the values, change the current,
> stop the emulator and change `config.yaml`. Only run the web UI on a trusted network, or set `host: 127.0.0.1` so only the computer
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
