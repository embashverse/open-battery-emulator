# Project

## Idea

A battery pack in software, running on a Raspberry Pi. Current goes in, the pack reacts
like a real battery: SOC falls or rises, cell voltages move, limits change. Pack data goes
out on CAN like a real BMS would send it. Signals only, no real power flows.

Everything is set in one file, `config.yaml`. The emulator runs on its own without a
screen. The web UI is optional: it shows live values, lets you change the current, and
can edit and save `config.yaml`. The file is always the one source of truth.

Current comes from one of two kinds of source, set in `config.yaml`:

- **An interface:** a real device (inverter, charger, test tool) sends the current, on
  CAN in v1, on UART or others later.
- **Simulated:** the emulator uses a fixed current from `config.yaml` (or changed live in
  the web UI), so it runs even with nothing else on the bus.

## Layout

```
open-battery-emulator/
├── README.md          how to run it
├── PROJECT.md         this file: idea, requirements, design, plan
├── AGENTS.md          rules for AI coding agents
├── config.yaml        all settings: battery, pack, current, CAN, web UI
├── pyproject.toml
├── batteries/         one folder per battery type, all CSV
│   └── lfp_100ah/
│       ├── cell.csv             capacity, R0, min and max cell voltage
│       ├── ocv_charge.csv       OCV per SOC and temperature, while charging
│       ├── ocv_discharge.csv    OCV per SOC and temperature, while discharging
│       ├── limit_charge.csv     max charge current per SOC and temperature
│       └── limit_discharge.csv  max discharge current per SOC and temperature
├── emulator/          the battery model in three levels
│   ├── cell.py        low:  battery CSVs, OCV, SOC, cell voltage, limits
│   ├── pack.py        mid:  cells, modules, strings, pack values, state machine
│   └── engine.py      high: config.yaml, current source, the 100 ms loop
├── interfaces/        how data leaves and enters, one folder each
│   └── can/
│       ├── __init__.py    exposes start, send, read_current, stop
│       ├── can_bus.py
│       ├── bring_up.py    brings the CAN bus up at boot (run as root)
│       └── battery.dbc    default message definitions, replace with your own
├── deploy/            run on the Pi as a service on boot
│   ├── open-battery-emulator-can.service   brings the bus up (root)
│   ├── open-battery-emulator.service       the emulator (normal user)
│   └── install.sh                          installs or removes both services
├── webui/             optional: live values, current, edit and save config.yaml
│   ├── __init__.py    exposes start, stop
│   ├── server.py      small web server (standard library), runs in a thread
│   └── static/        index.html, style.css, app.js (no build step, no internet)
└── tests/             test_cell.py, test_pack.py, test_engine.py, ...
```

Add a battery: copy a folder in `batteries/`. Add an interface (UART, isoSPI, Modbus...):
add a folder in `interfaces/` and a section under `interfaces:` in `config.yaml`. Neither
needs a change in `emulator/`, and the web UI shows both automatically.

### Interface contract

Every interface is a Python package in `interfaces/` whose `__init__.py` exposes the
same four functions:

```
start(settings, pack_info)  open the port or bus. settings is its section of
                            config.yaml; pack_info is the pack shape and its highest
                            values (cells, modules, strings, v_max, current limits),
                            so the interface can check at start that the pack fits.
                            Returns True if this interface can provide current
                            commands, otherwise False
send(result)                send the latest pack result (called every tick, 100 ms)
read_current()              latest current command, or None if this interface has no input
stop()                      close the port or bus cleanly (called once on exit)
```

The engine loads each interface with `enabled: true` by its name under `interfaces:` in
`config.yaml`, importing `interfaces.<name>` with `importlib`. It calls these four
functions and nothing else, so `engine.py` never imports anything CAN or UART specific.
If `current.source` names an interface whose `start()` returned False, the engine stops
every started interface and the start stops with an error naming `current.source`. CAN
returns True only if the DBC has a `CurrentCommand` signal.

Before calling `start`, the engine makes relative file paths full: any relative string
setting that names an existing file next to `config.yaml` (like `dbc:`) is passed with its
full path, so it works whatever folder the emulator is started from. Every other setting
is passed unchanged.

### Web UI contract

The web UI is a Python package `webui/` whose `__init__.py` exposes two functions:

```
start(host, port, handle)   start the web server in a background thread. handle has
                            two functions: read_state() returns the latest pack result
                            and live settings as a plain dict, send_command(command)
                            checks one command and queues it (raises CommandError if
                            invalid). Returns the port really used (port 0 = any free one)
stop()                      stop the server and its thread (called once on exit)
```

The engine imports `webui` with `importlib`, only when `webui.enabled` is true, so with
the web UI off it is never loaded. It passes the handle, never the Engine itself, so the
web thread cannot change the pack (REQ-34). Commands are applied by the engine at the
start of the next tick.

## config.yaml

```yaml
battery: lfp_100ah

pack:
  cells_per_module: 12
  modules_in_series: 8
  strings_in_parallel: 2

start:
  soc: 80                  # percent
  temperature_c: 25

variation:                 # natural spread between cells, all 0 = identical cells
  capacity_pct: 2          # each cell's capacity within +-2 % of the battery file
  r0_pct: 5
  soc_pct: 1
  seed: 42                 # same seed = same pack every run

current:
  source: can        # simulated, or the name of an enabled interface (can)
  simulated_a: 50          # discharge positive
  timeout_ms: 500
  deadband_a: 0.5          # pack currents within +-0.5 A count as 0

interfaces:
  can:
    enabled: true
    driver: socketcan      # socketcan, or virtual (python-can's virtual bus, tests and CI)
    channel: vcan0         # can0 on the Pi
    bitrate: 500000
    dbc: interfaces/can/battery.dbc
    module_frames:
      base_id: 0x200
      id_step: 0x10        # IDs per module
      cells_per_frame: 4
      cycle_ms: 1000
  # uart:                  # example of a future interface
  #   enabled: false
  #   port: /dev/ttyUSB0
  #   baudrate: 115200

webui:
  enabled: false
  host: 0.0.0.0            # 0.0.0.0 = reachable from other devices, 127.0.0.1 = this computer only
  port: 8080
```

Settings are YAML because they are grouped key value pairs. Battery data is CSV because it
is tables you may want to edit in a spreadsheet.

## How the emulator works

Three levels, each only uses the one below it:

| Level | File | Knows about | Does |
|---|---|---|---|
| High | `engine.py` | config, current source, time | reads `config.yaml`, picks the current (CAN or simulated), runs the loop, hands results to interfaces |
| Mid | `pack.py` | many cells | builds strings and modules, splits current, finds min and max cell, runs the state machine |
| Low | `cell.py` | one cell | reads the battery CSVs, OCV lookup, SOC by coulomb counting, cell voltage, current limits |

Every 100 ms:

```
engine.py   get current (CAN or simulated)
  pack.py     deadband: small currents count as 0
  pack.py     state machine: block current if FULL or EMPTY
  pack.py     split current across strings by resistance
    cell.py     update SOC of each cell
    cell.py     cell voltage = OCV(SOC) - I x R0
  pack.py     pack, string, module values, min and max cell, state
    cell.py     allowed charge and discharge current
engine.py   hand the result to every enabled interface and the web UI
```

### State machine

| From | Condition | To |
|---|---|---|
| IDLE, CHARGING | I > 0 | DISCHARGING |
| IDLE, DISCHARGING | I < 0 | CHARGING |
| CHARGING, DISCHARGING | I = 0 | IDLE |
| DISCHARGING | lowest cell <= `v_min` | EMPTY |
| CHARGING | highest cell >= `v_max` | FULL |
| EMPTY | I < 0 (charge) | CHARGING |
| FULL | I > 0 (discharge) | DISCHARGING |

Anything not in the table keeps the current state.

- **IDLE:** no current.
- **CHARGING / DISCHARGING:** current flows, SOC changes.
- **FULL:** the highest cell reached `v_max`. Charging is blocked. The state stays FULL
  until a discharge current flows, then it goes to DISCHARGING.
- **EMPTY:** the lowest cell reached `v_min`. Discharging is blocked. The state stays
  EMPTY until a charge current flows, then it goes to CHARGING.

Blocking current is what a real BMS does by opening its contactors. Leaving FULL or EMPTY
only when the current changes direction stops the state from flickering when a charger
keeps pushing at full.

### Test controls

Two shortcuts for testing, available from the engine and used by the web UI:

- **Set SOC:** jump every cell to a chosen SOC (for example 100 % instead of charging
  for an hour). The cell spread is kept.
- **Hold SOC:** freeze SOC while current keeps flowing, so the pack stays at one operating
  point. Voltages still show the I x R0 drop.

## Battery files

Values are per cell. A "cell" is one series element: if a module has cells in parallel,
enter the combined values. The `lfp_100ah` values are a generic placeholder, to be
replaced with real data.

`cell.csv`
```
parameter,value,unit
capacity,100,Ah
r0,0.001,ohm
v_min,2.50,V
v_max,3.65,V
```

The four tables share one shape: SOC in % as rows, temperature in °C as columns. You can
give every SOC step or just a few points; values in between are linearly interpolated in
both SOC and temperature. This minimal table is valid:

```
soc,0,25,45
0,2.80,2.80,2.81
10,3.19,3.20,3.20
90,3.31,3.32,3.32
100,3.44,3.45,3.45
```

Table rules:
- SOC rows and temperature columns must be in ascending order, no duplicates.
- Below the first or above the last SOC row, the nearest row is used.
- A table with one temperature column ignores temperature.

`ocv_discharge.csv` (volts), same for `ocv_charge.csv`
```
soc,-20,-10,0,10,25,45,55
0,2.791,2.793,2.795,2.797,2.800,2.804,2.806
1,2.891,2.893,2.895,2.897,2.900,2.904,2.906
...
100,3.441,3.443,3.445,3.447,3.450,3.454,3.456
```

`limit_charge.csv` (amps per cell), same for `limit_discharge.csv`
```
soc,-20,-10,0,10,25,45,55
0,0.0,0.0,0.0,30.0,100.0,100.0,30.0
...
100,0.0,0.0,0.0,0.0,0.0,0.0,0.0
```

Temperature rules:
- To add a temperature, add a column. The first and last column are the operating range.
- Outside the operating range, both current limits are 0 and a temperature flag is set.
  OCV uses the nearest column.
- Charging uses the charge OCV, discharging the discharge OCV. IDLE keeps the curve of the
  last current direction (discharge curve at start). This gives LFP like hysteresis.

## CAN

Pack messages are defined entirely by the DBC file: IDs, byte layout, scaling and cycle
time (`GenMsgCycleTime`). To match your own system, edit the DBC or point `can.dbc` to
your own file. The rules:

- Signals must use the emulator's names below, so the emulator knows what to put where.
  An unknown name stops the start with an error naming the signal.
- Every message the emulator sends needs a `GenMsgCycleTime` that is a multiple of
  100 ms (one tick). A message holds either signals the emulator sends or signals it
  receives, never both.
- Physical values are in V, A and %, like the pack result (so cell voltages use a factor
  of 0.001 V, which is 1 mV per bit).
- `PackState` needs `VAL_` names for IDLE, CHARGING, DISCHARGING, FULL and EMPTY.
- Each signal's range must hold the values this pack can reach (for example pack voltage
  up to cells in series x `v_max`), checked at start. `AliveCounter` counts 0 to 255 and
  wraps.

`interfaces.can.driver` is `socketcan` (real or `vcan` interfaces) or `virtual`
(python-can's in-memory bus, used by the tests and CI).

| Signal name | Meaning |
|---|---|
| PackVoltage, PackCurrent, PackSOC | pack values |
| PackState, CurrentTimeout, TempOutOfRange, AliveCounter | status |
| CellVmax, CellVmaxModule, CellVmaxCell | highest cell and where |
| CellVmin, CellVminModule, CellVminCell | lowest cell and where |
| MaxChargeCurrent, MaxDischargeCurrent | limits |
| CurrentCommand | input from the device (read, not sent) |

Default DBC:

| ID | Dir | Content | Cycle |
|---|---|---|---|
| 0x100 | out | pack voltage, current, SOC, state, counter | 100 ms |
| 0x101 | out | min and max cell voltage and which cell | 100 ms |
| 0x102 | out | max charge and discharge current | 100 ms |
| 0x180 | in | current command (0.1 A, discharge positive) | from the device |

Module frames depend on pack size, so they follow a pattern set in `config.yaml` instead
of the DBC. With the defaults:

```
0x200 + (module-1) x 0x10 + n     cell voltages, 4 cells per frame, uint16, 1 mV
0x200 + (module-1) x 0x10 + 0xF   module temperature and status
```

Modules are numbered string by string: with 8 in series, module 9 is the first module of
string 2. Frame n holds cells 4n+1 to 4n+4. Little endian.

The status frame at offset 0xF:

| Bytes | Content |
|---|---|
| 0-1 | module temperature, int16, 0.1 °C (the pack temperature for now) |
| 2 | flags: bit 0 = temperature out of range, other bits 0 |
| 3-7 | unused, 0xFF |

Every module frame, including 0xF, always has DLC 8. Unused bytes are 0xFF, for example
the end of a last cell frame with fewer cells. Checked at start: the cell frames of a
module must fit below offset 0xF, `id_step` must be at least 0x10 so a module never
overlaps the next one, the highest ID must be at most 0x7FF, and no module frame may use a
DBC message ID.

## Requirements

Settings
- **REQ-01** All settings are in `config.yaml`. The emulator runs from it alone.

Battery
- **REQ-02** A battery type is a folder in `batteries/` with `cell.csv` and four tables:
  `ocv_charge.csv`, `ocv_discharge.csv`, `limit_charge.csv`, `limit_discharge.csv`, each
  with SOC in % as rows and temperatures as columns. Any number of rows and columns is
  allowed. A new battery, an extra row or an extra column needs no code change.
- **REQ-23** Table values between rows and columns are found by linear interpolation in
  SOC and temperature. Outside the SOC rows the nearest row is used. A table that is not
  in ascending order is rejected at start with a clear error naming the file.

Pack
- **REQ-03** Pack = P strings in parallel, each of S modules in series, each of C cells in
  series.
- **REQ-04** Discharge current is positive. SOC is in percent everywhere (files, config,
  code). It changes by coulomb counting and stays between 0 and 100 %.
- **REQ-05** Cell voltage = OCV(SOC, temperature) - I x R0, using the charge or discharge
  OCV table by current direction as described in Battery files. The OCV curve of every
  cell follows the direction of the pack current; at 0 A each cell keeps its last curve,
  so circulating currents between strings never flip curves. Current splits between
  parallel strings by resistance. All strings share one pack voltage:
  V_pack = (Σ OCV_k/R_k − I_pack) / (Σ 1/R_k), and I_k = (OCV_k − V_pack) / R_k, where
  OCV_k and R_k are the sums over the string's cells at the start of the tick. If any
  string's R_k is 0, current splits equally.
- **REQ-06** Allowed charge and discharge current per cell come from the limit tables at
  the cell's SOC and the set temperature, interpolated in both. The pack charge and
  discharge limits are P times the lowest cell limit; the temperature flag is set if any
  cell is out of range.
- **REQ-22** When the limit tables have two or more temperature columns, the first and last
  define the operating range. Outside it, both current limits are 0 and a temperature flag
  is set.
- **REQ-20** Each cell gets its own capacity, R0 and start SOC, spread randomly around the
  battery file values by the percentages in `variation`. Capacity and R0 spread by a
  percent of the file value, SOC by SOC percentage points. The same seed gives the same
  pack every run. All percentages 0 gives identical cells.

State
- **REQ-07** The emulator has the states IDLE, CHARGING, DISCHARGING, FULL and EMPTY with
  the transitions in the state table. FULL blocks charging and is left only by a
  discharge current; EMPTY blocks discharging and is left only by a charge current.
- **REQ-21** The engine offers Set SOC (jump all cells to a SOC, keeping the spread) and
  Hold SOC (freeze SOC while current flows). The web UI shows both as controls.

Current input
- **REQ-08** Current is simulated or comes from an interface (CAN in v1), as set by
  `current.source` in `config.yaml`: `simulated` or the name of an enabled interface
  whose `start()` says it can provide current commands. Anything else stops the start
  with an error naming `current.source`.
- **REQ-09** Simulated current behaves exactly like a current command arriving every
  100 ms.
- **REQ-10** If the source is an interface and no current command arrives within
  `timeout_ms`, current is 0 and the timeout flag is set. The timeout is handled in
  `engine.py` for any interface, never in interface code. The last command is held
  until more than `timeout_ms` has passed since it arrived. At start no command has
  arrived, so the emulator starts in timeout (0 A, flag set). The flag clears as soon as
  a command arrives.
- **REQ-24** Pack currents within ±`current.deadband_a` count as 0. The deadband is
  applied once per tick in `pack.py` before anything else, and only to the pack current,
  never to string currents.

CAN
- **REQ-11** Driver (`socketcan` or `virtual`), channel, bitrate and DBC file are set
  under `interfaces.can` in `config.yaml`.
- **REQ-12** Pack messages are built from the DBC file using the signal names above.
  Changing IDs, layout, scaling or cycle time needs only a DBC edit.
- **REQ-13** Module frames follow the pattern set in `config.yaml`.
- **REQ-27** A value outside its CAN signal range (DBC signals and module frame fields)
  is clamped to the range and a warning is printed once per signal. A received frame that
  cannot be decoded is skipped with a warning. The emulator never stops because of a
  value.
- **REQ-28** If the bus refuses a frame (for example no other device acknowledges on
  `can0`), the emulator keeps running. It prints one warning when sending starts failing
  and one when it works again.

Structure
- **REQ-14** Every interface follows the interface contract (`start`, `send`,
  `read_current`, `stop`) and is switched on in `config.yaml`. The engine loads it by
  name with `importlib` and never imports anything interface specific. Adding one needs
  no change in `emulator/`. The web UI only reads the result and queues commands
  (REQ-34).
- **REQ-26** When any interface is enabled, `--speed` must be 1, because devices on the
  bus expect real time cycle times. Otherwise the start stops with an error naming
  `--speed`.

Terminal
- **REQ-25** When run from the command line, the engine prints one status line per
  second of emulated time with elapsed time, state, pack current, voltage, average SOC,
  and lowest and highest cell with module and cell number. It shows `TIMEOUT` when the
  timeout flag is set. `--status-every N` prints the status line every N seconds of
  emulated time (default 1).

Web UI
- **REQ-15** The web UI is optional. When enabled it shows current, SOC, voltages, state
  and limits live, and can change the source and the simulated current. When
  `webui.enabled` is true, the engine starts a small web server in a background thread in
  the same process. When false, nothing about the engine changes. `webui.host` (default
  `0.0.0.0`) and `webui.port` (default 8080) come from `config.yaml`. Live changes from
  the page (source, simulated current, Set SOC, Hold SOC) are not saved to `config.yaml`,
  and the page says so.
- **REQ-34** Only the engine thread changes the pack. The web server only reads the
  latest result and puts commands in a queue; the engine applies queued commands at the
  start of the next tick. The web UI gets a handle with two functions (read the state,
  queue a command), never the Engine itself.
- **REQ-35** The page works without internet: no external scripts, styles or fonts.
- **REQ-16** `config.yaml` is the only place settings are stored. The web UI loads its
  settings form from the file, and a Save button checks the values and writes them back
  to the file, keeping comments. The emulator then restarts with the new settings.
- **REQ-17** If `config.yaml` was edited by hand while running, the web UI shows that the
  file changed and offers to reload it, so the UI never shows stale values.
- **REQ-18** The web UI builds its choices from what exists: batteries from the folders in
  `batteries/`, interfaces and their settings from `config.yaml`. A new battery or
  interface appears in the UI without UI code changes.
- **REQ-29** The web UI lets the user change the CAN ID of each message in the DBC
  (PackStatus, CellStats, PackLimits, CurrentCommand) and the module frame `base_id` and
  `id_step` in `config.yaml`. Signal names and layouts are not editable in the UI. Save
  writes the IDs into the DBC file and `config.yaml`. Save is refused with a clear message
  if two messages share an ID, a pack message falls inside the module frame range, an ID
  is above 0x7FF, or the module range does not fit the pack. The UI shows the module ID
  range for the current pack size and notes that lower IDs have higher priority on the
  bus.

Platform
- **REQ-19** Runs on Linux with SocketCAN: `vcan0` on a laptop, `can0` on the Pi.

Deployment
- **REQ-30** The CAN bus is brought up at boot by a separate step run as root, reading
  channel and bitrate from `config.yaml`. For a channel starting with `vcan` it loads the
  vcan module and creates the device; otherwise it sets the bitrate with `restart-ms 100`,
  so the controller recovers from bus off automatically, and brings the device up.
- **REQ-31** The emulator starts at boot as a normal user (never root), only after the bus
  is up, and restarts automatically if it stops with an error.
- **REQ-32** One install script sets up both services on a Pi, and can also remove them.
- **REQ-33** The engine stops cleanly on SIGTERM as well as Ctrl+C, calling `stop()` on
  every interface.

## How we work (light V-model)

Every requirement on the left has a check on the right:

| Define | Check |
|---|---|
| Idea | Runs on the Pi with a real device on the bus (v1.0) |
| Requirements | Each REQ has at least one test, or a manual check where a test is not possible |
| High level (`engine.py`) | `test_engine.py`: run many ticks from a config, check the whole result |
| Mid level (`pack.py`) | `test_pack.py`: topology, current split, state machine |
| Low level (`cell.py`) | `test_cell.py`: CSV loading, OCV, SOC, voltage, limits |

Rules:
1. Every feature starts as a requirement in this file.
2. Every test names the requirement it checks: `"""Checks REQ-04."""`
   See coverage with `grep -rn "REQ-" tests/`
3. One task per branch, merged by pull request. CI must be green.
4. AI writes code, a human reviews and understands it before merging.
5. Every requirement is verified by an automated test where possible, otherwise by a
   manual check recorded in the table below.

### Manual checks

Some things only a real Pi with systemd can show: booting, rebooting, crashes. These are
run by hand, following README "Run on a Raspberry Pi", and recorded here. The unit file
contents are also checked automatically in `tests/test_deploy.py`.

| REQ | Steps | Expected | Date | Result |
|---|---|---|---|---|
| REQ-32 | `sudo deploy/install.sh` | both services installed, enabled and started, no errors |19.11.2026 |Pass |
| REQ-31 | `systemctl status open-battery-emulator` | active (running), as the normal user (not root); `journalctl -u open-battery-emulator` shows a status line every 10 s |19.11.2026 |Pass |
| REQ-31 | `sudo reboot`, log in, `systemctl status open-battery-emulator` | running again after the reboot, `candump vcan0` shows frames |19.11.2026 |Pass |
| REQ-31 | `sudo systemctl kill -s KILL open-battery-emulator`, wait, `systemctl status open-battery-emulator` | running again within about 5 s, with a new PID |19.11.2026 |Pass |
| REQ-32 | `sudo deploy/install.sh --remove` | both services stopped and disabled, unit files gone from `/etc/systemd/system` |19.11.2026 |Pass |
| REQ-15 | `webui.enabled: true`, start the emulator, open `http://localhost:8080` on the laptop | values update about twice a second, the cell grid shows every module, Set SOC / Hold SOC / simulated current change the values on the next update, SOC 150 shows an error | | |
| REQ-15 | on the Pi, open `http://battery.local:8080` on a phone on the same network | page readable without zooming, controls usable, tapping a cell shows its voltage | | |

## Milestones

| # | Goal | Done when |
|---|---|---|
| M0 | Setup | skeleton and green CI on `main` |
| M1 | Cell and pack | `cell.py` and `pack.py` pass their tests; 100 A for 1 h on a 100 Ah cell takes SOC from 100 % to 0 %; every state transition, cell variation, Set SOC and Hold SOC tested |
| M2 | Engine | engine runs in the terminal from `config.yaml` with simulated current and prints values |
| M3 | CAN | M3a: pack messages on `vcan0` decode with the DBC; module frames follow the pattern. M3b: 0x180 changes the current; timeout works |
| M4 | Pi | runs on the Pi as a service on boot with `vcan0`, survives a reboot, restarts after a crash |
| M5 | Web UI | optional page shows live values, changes the simulated current, saves settings to `config.yaml`, and edits CAN message IDs and module frame IDs with checks before saving (REQ-29) |
| v1.0 | Release | real hardware test with an MCP2515 HAT on the Pi and a CANable on a laptop (frames received and current sent over real wires), README quick start works, every REQ is verified: a passing test, or a manual check recorded in PROJECT.md |

## Later

Temperature model (temperature changing with current), fault injection, NMC and other
batteries, gradual hysteresis transition (OCV moves from one curve to the other over a
few % SOC instead of switching at once), per module capacity, load profiles from CSV, script that writes module frames
into a DBC for decoding tools, UART interface, isoSPI interface (needs an LTC6820 type
transceiver and tight timing, likely on an MCU rather than Linux), Modbus or MQTT for BESS.
