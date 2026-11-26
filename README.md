# Open Battery Emulator

A battery pack in software for the Raspberry Pi. Current goes in, the pack reacts like a
real battery, and BMS style data goes out on CAN and UART. Signals only, no real power.

## What it does

- Battery types as simple CSV files: add your own by copying a folder
- Any pack size: cells per module, modules in series, strings in parallel
- Current from a real device on CAN or UART, or simulated
- SOC, cell voltages, limits and a BMS like state machine (full, empty, charging...)
- CAN messages defined by a DBC file you can swap for your own
- Simple checksummed text lines on a serial port (UART)
- All settings in one `config.yaml`, a web UI for live values, Play / Stop and settings

## What's inside

- `config.yaml` all settings
- `batteries/` battery types
- `emulator/` the model: `cell.py`, `pack.py`, `config.py`, `engine.py`
- `interfaces/` CAN and UART, more later
- `webui/` live view, Play / Stop and settings in the browser
- `deploy/` quick install and the services for the Pi

Requirements, design and plan: [PROJECT.md](PROJECT.md)

## Quick install (Raspberry Pi)

1. With [Raspberry Pi Imager](https://www.raspberrypi.com/software/) write **Raspberry Pi
   OS Lite (64-bit)**. In the OS customisation set a hostname (for example `battery`), a
   user and password, your Wi‑Fi, and enable SSH.
2. Boot the Pi and log in from your laptop: `ssh <user>@battery.local`
3. Run the quick install. Once the repository is public, one command does everything:

   ```
   curl -LsSf https://raw.githubusercontent.com/embashverse/open-battery-emulator/main/deploy/bootstrap.sh | bash
   ```

   Today, from a clone (`gh repo clone embashverse/open-battery-emulator` while the
   repository is private):

   ```
   cd open-battery-emulator
   deploy/bootstrap.sh
   ```

`bootstrap.sh` installs git and can-utils, installs [uv](https://docs.astral.sh/uv/) if
missing, uses the clone it is run from (or clones into `~/open-battery-emulator`), runs
`uv sync` and `sudo deploy/install.sh`. It is safe to run twice and stops at the first
error. At the end it prints the web UI address, like `http://battery.local:8080`: open
it from a laptop or phone on the same network.

The emulator now runs as a service and starts at every boot (details in
[On the Pi](#on-the-pi)).

## Normal install (laptop)

Needs Linux, Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/embashverse/open-battery-emulator.git
cd open-battery-emulator
uv sync
uv run python -m emulator.engine
```

Open `http://localhost:8080`. Stop it with Ctrl+C.

Options: `--config other.yaml` (default `config.yaml`), `--speed 10` (10 times faster
than real time; must be 1 while an interface such as CAN is enabled) and
`--status-every 10` (print the status line every 10 s instead of every second).

## CAN and UART

CAN and UART are off by default, so a fresh install has nothing that can fail. Enable them
in the web UI Config tab or in `config.yaml`. To try them without hardware (virtual CAN
or a virtual UART), see [Testing without hardware](#testing-without-hardware).

What is needed at boot:

- **CAN on the Pi with the service:** the CAN bus is brought up automatically at every
  boot when CAN is enabled (`can0` with its bitrate, or `vcan0` created). Nobody types
  `ip link` there. After changing `enabled`, `channel` or `bitrate`, restart once, or
  reboot:

  ```
  sudo systemctl restart open-battery-emulator-can open-battery-emulator
  ```

- **A CAN HAT** needs a one time device tree overlay line in `/boot/firmware/config.txt`,
  then a reboot. For an MCP2515 HAT for example
  `dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25`; the oscillator and
  interrupt values depend on the HAT, check its documentation.
- **UART** needs nothing at boot: a USB adapter (`/dev/ttyUSB0`) appears when plugged in.
  The Pi's own UART (`/dev/serial0`, GPIO 14 and 15) needs a one time setting and the
  `dialout` group:
  1. `sudo raspi-config` → Interface Options → Serial Port: login shell over serial
     **No**, serial port hardware **Yes**. Reboot.
  2. `sudo usermod -aG dialout $USER`, then
     `sudo systemctl restart open-battery-emulator`.
- **On a laptop without the service:** `vcan0` must be created by hand after every reboot,
  and a virtual UART (`socat`) is always started by hand. Both are in
  [Testing without hardware](#testing-without-hardware).

If CAN is enabled but the bus cannot be brought up (no HAT, wrong channel), the emulator
still starts: it stays STOPPED and the web UI shows the error, so you can fix it there.

## Web UI

A page in the browser with two tabs. It needs no internet.

- **Live:** SOC, voltage, current, state, lowest and highest cell, limits and flags, a
  grid of every cell (hover or tap for its voltage), and controls: current source,
  simulated current, Set SOC, Hold SOC and one Play / Stop button.
- **Config:** every setting in `config.yaml`, grouped, with Save.

To edit settings: press **Stop**, change values in **Config**, **Save** (checked first,
comments in `config.yaml` are kept), then **Play**. If Play fails (a bad setting, a missing
CAN channel...) it stays STOPPED and shows the error under the run button. Changes made
with the Live controls are not saved. `webui.host` and `webui.port` take effect after a
restart of the program.

`start.autostart: true` (the default) presses Play at start, so the Pi is on the bus right
after boot. Set `webui.enabled: false` to run without the page.

> **No login.** Anyone who can reach the port can see the values, change the current,
> stop the emulator and change `config.yaml`. Only run the web UI on a trusted network,
> or set `host: 127.0.0.1` so only the computer itself can open it.

## On the Pi

Two services are installed: `open-battery-emulator-can` brings the CAN bus up at boot (as
root, only when CAN is enabled), then `open-battery-emulator` runs the emulator as your
normal user and restarts it if it stops with an error.

```
systemctl status open-battery-emulator      # should say "active (running)"
journalctl -u open-battery-emulator -f      # status line every 10 s, Ctrl+C to leave
```

**Editing and developing on a quick install Pi:** `~/open-battery-emulator` is a normal
git clone. After changing code or `config.yaml` by hand, restart the service:
`sudo systemctl restart open-battery-emulator`.

**Update:**

```
cd ~/open-battery-emulator
git pull
uv sync
sudo systemctl restart open-battery-emulator
```

**Remove:** `sudo deploy/install.sh --remove` stops and disables both services and
deletes the unit files.

## Testing without hardware

> These steps will move to `docs/`.

### The status line

```
t=     1.0 s  DISCHARGING  I=   50.0 A  V=  315.36 V  SOC= 80.00 %  min 3.283 V (M7 C5)  max 3.287 V (M16 C6)
```

`t` emulated time, the state, `I` the current that really flowed (discharge positive),
`V` pack voltage, `SOC` average SOC, `min` / `max` lowest and highest cell with module
(`M`) and cell (`C`). `TIMEOUT` at the end: the current comes from an interface and no
command arrived within `current.timeout_ms`, so the current is 0.

### Virtual CAN

Set `interfaces.can.enabled: true` and `channel: vcan0`. On a laptop create `vcan0` after
every boot:

```
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

or let the project do the same from `config.yaml`:
`sudo .venv/bin/python -m interfaces.can.bring_up`. On the Pi the service does this at
boot (restart it after changing the channel, see [CAN and UART](#can-and-uart)).

Watch the frames (`sudo apt install can-utils`): 0x100 to 0x102 ten times a second,
module frames from 0x200 once a second.

```
candump vcan0
candump vcan0 | uv run python -m cantools decode interfaces/can/battery.dbc
```

**Send a current:** set `current.source: can`. Until a command arrives it runs at 0 A
with `TIMEOUT`. The command is 0x180 (`CurrentCommand` in the DBC): signed 16 bit, 0.1 A,
little endian, discharge positive. 50 A = 500 = 0x01F4, sent as `F4 01`. One frame holds
for `timeout_ms`, so repeat it:

```
while true; do cansend vcan0 180#F401000000000000; sleep 0.1; done   # 50 A discharge
while true; do cansend vcan0 180#38FF000000000000; sleep 0.1; done   # 20 A charge
```

A value that does not fit its signal is clamped and warned about once. If the bus refuses
frames (no other device on `can0` to acknowledge them), the emulator warns and keeps
running.

### Virtual UART

The lines, fields and checksum are in PROJECT.md "UART". `socat` makes a pair of
connected virtual serial ports (`sudo apt install socat picocom`). Keep it running in its
own terminal:

```
socat -d -d pty,raw,echo=0,link=/tmp/ttyOBE0 pty,raw,echo=0,link=/tmp/ttyOBE1
```

Set `interfaces.uart.enabled: true` and `port: /tmp/ttyOBE0`, start the emulator, and
watch the other end (quit picocom with Ctrl+A, then Ctrl+X):

```
picocom -b 115200 /tmp/ttyOBE1
```

```
$PACK,315.36,50.0,80.00,DISCHARGING,0,0*79
$CELL,3.283,7,5,3.287,16,6*31
$LIM,200.0,200.0*48
```

**Send a current:** set `current.source: uart` and repeat the line:

```
while true; do printf '$CUR,50*6D\r\n' > /tmp/ttyOBE1; sleep 0.1; done    # 50 A discharge
while true; do printf '$CUR,-20*47\r\n' > /tmp/ttyOBE1; sleep 0.1; done   # 20 A charge
```

A line with a wrong checksum or format is ignored with one warning. If the port goes away
(a USB adapter unplugged), the emulator keeps running and reopens it every 2 seconds.

### The Pi's UART with a USB adapter

After the one time setup in [CAN and UART](#can-and-uart), set `port: /dev/serial0`.
Wire a 3.3 V USB serial adapter (for example a CP2102) to the laptop: Pi TX (GPIO 14,
pin 8) to adapter RX, Pi RX (GPIO 15, pin 10) to adapter TX, GND (pin 6) to GND. Do not
connect the adapter's 3.3 V or 5 V pin. On the laptop: `picocom -b 115200 /dev/ttyUSB0`.
