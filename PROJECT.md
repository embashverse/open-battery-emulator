# Project

## Idea

A battery pack in software, running on a Raspberry Pi. Current goes in, the pack reacts
like a real battery: SOC falls or rises, cell voltages move, limits change. Pack data goes
out on CAN and UART like a real BMS would send it. Signals only, no real power flows.

Everything is set in one file, `config.yaml`. The emulator runs on its own without a
screen. The web UI is optional: it shows live values, lets you change the current, has
a Play / Stop button, and can edit and save `config.yaml`. The file is always the one
source of truth.

Current comes from one of two kinds of source, set in `config.yaml`:

- **An interface:** a real device (inverter, charger, test tool) sends the current, on
  CAN or UART, others later.
- **Simulated:** the emulator uses a fixed current from `config.yaml` (or changed live in
  the web UI), so it runs even with nothing else on the bus.

## Layout

```
open-battery-emulator/
├── README.md          how to run it
├── PROJECT.md         this file: idea, requirements, design, plan
├── AGENTS.md          rules for AI coding agents
├── config.yaml        all settings: battery, pack, current, CAN, UART, web UI
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
│   ├── config.py      high: config.yaml: read, check, settings form, battery list, save
│   └── engine.py      high: current source, run state, the 100 ms loop
├── interfaces/        how data leaves and enters, one folder each
│   └── can/
│       ├── __init__.py    exposes start, send, read_current, stop
│       ├── can_bus.py
│       ├── bring_up.py    brings the CAN bus up at boot (run as root)
│       └── battery.dbc    default message definitions, replace with your own
│   └── uart/
│       ├── __init__.py    exposes start, send, read_current, stop
│       └── uart_port.py   text lines on a serial port
├── deploy/            run on the Pi as a service on boot
│   ├── open-battery-emulator-can.service   brings the bus up (root)
│   ├── open-battery-emulator.service       the emulator (normal user)
│   ├── install.sh                          installs or removes both services
│   └── bootstrap.sh                        quick install on a fresh Pi (apt, uv, clone, install.sh)
├── webui/             optional: live values, Play / Stop, edit and save config.yaml
│   ├── __init__.py    exposes start, stop
│   ├── server.py      small web server (standard library), runs in a thread
│   └── static/        index.html, style.css, app.js (no build step, no internet)
└── tests/             test_cell.py, test_pack.py, test_engine.py, ...
```

Add a battery: copy a folder in `batteries/`. Add an interface (isoSPI, Modbus...):
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
                            three functions:
                            read_state() returns the latest pack result, live settings,
                              run state, last error and the results of recent commands
                              as a plain dict
                            send_command(command) checks one command, queues it and
                              returns its id (raises CommandError if invalid)
                            read_config() returns the settings form built from
                              config.yaml (only reads the file)
                            Returns the port really used (port 0 = any free one)
stop()                      stop the server and its thread (called once on exit)
```

The engine imports `webui` with `importlib`, only when `webui.enabled` is true, so with
the web UI off it is never loaded. It passes the handle, never the Engine itself, so the
web thread cannot change the pack (REQ-34). Commands are applied by the engine at the
start of the next pass of the loop. Play, Stop and Save are commands too. The engine
reports each command's result (`{id, command, ok, message}`) in `read_state()["actions"]`,
so the page shows it. The web UI has no code for any particular interface: the Config tab
is built from whatever `config.yaml` holds.

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
  autostart: true          # false = start STOPPED, press Play in the web UI

variation:                 # natural spread between cells, all 0 = identical cells
  capacity_pct: 2          # each cell's capacity within +-2 % of the battery file
  r0_pct: 5                # each cell's R0 within +-5 % of the battery file
  soc_pct: 1               # each cell's start SOC within +-1 SOC point
  seed: 42                 # same seed = same pack every run

current:
  source: simulated        # simulated, or the name of an enabled interface (can, uart)
  simulated_a: 50          # discharge positive
  timeout_ms: 500
  deadband_a: 0.5          # pack currents within +-0.5 A count as 0

# CAN and UART are off by default, so a fresh install has nothing that can fail.
# Turn them on here or in the web UI Config tab.
# - On the Pi with the service, the CAN bus is brought up at every boot when CAN is
#   enabled (can0 with its bitrate, or vcan0 created). After changing enabled,
#   channel or bitrate, restart once (or reboot):
#     sudo systemctl restart open-battery-emulator-can open-battery-emulator
#   A CAN HAT also needs a one time dtoverlay line in the Pi's boot config (README).
# - UART needs nothing at boot: a USB adapter appears when plugged in. The Pi's own
#   UART needs a one time raspi-config setting and the dialout group (README).
# - On a laptop without the service, create vcan0 by hand after every reboot, and
#   start a virtual UART (socat) by hand (README "Testing without hardware").
interfaces:
  can:
    enabled: false
    driver: socketcan      # socketcan, or virtual (python-can's virtual bus, tests and CI)
    channel: can0          # can0 for a real adapter, vcan0 for virtual CAN
    bitrate: 500000
    dbc: interfaces/can/battery.dbc
    module_frames:
      base_id: 0x200
      id_step: 0x10        # IDs per module
      cells_per_frame: 4
      cycle_ms: 1000
  uart:
    enabled: false
    port: /dev/ttyUSB0     # a USB adapter, /dev/serial0 for the Pi's own UART
    baudrate: 115200
    cycle_ms: 1000         # a multiple of 100
    modules: false         # true = also send one $MOD line per module

webui:                     # no login: only run it on a trusted network
  enabled: true
  host: 0.0.0.0            # 0.0.0.0 = reachable from other devices, 127.0.0.1 = this computer only
  port: 8080
```

Settings are YAML because they are grouped key value pairs. Battery data is CSV because it
is tables you may want to edit in a spreadsheet.

## How the emulator works

Three levels, each only uses the one below it:

| Level | File | Knows about | Does |
|---|---|---|---|
| High | `config.py` | `config.yaml` | reads and checks it, builds the settings form and battery list for the web UI, saves it. Only `engine.py` uses it |
| High | `engine.py` | current source, time, run state | Play and Stop, picks the current (an interface or simulated), runs the loop, hands results to interfaces |
| Mid | `pack.py` | many cells | builds strings and modules, splits current, finds min and max cell, runs the state machine |
| Low | `cell.py` | one cell | reads the battery CSVs, OCV lookup, SOC by coulomb counting, cell voltage, current limits |

### Run states

| State | Ticks | Interfaces | Settings |
|---|---|---|---|
| STOPPED | none | stopped, so no CAN frames | editable in the web UI |
| RUNNING | every 100 ms | started, frames sent | locked |

The loop and the web server keep running in both states. **Play** reads the saved
`config.yaml`, runs every start check, builds a new pack and starts the interfaces; time
and live settings start over. If anything fails, the emulator stays STOPPED and the page
shows the error. **Stop** stops the interfaces; the last values stay on the page.
`start.autostart: true` (the default) presses Play at program start, so the Pi service
puts the battery on the bus at boot. Ctrl+C and SIGTERM stop everything in either state.

If Play fails at program start and the web UI is on, the emulator stays STOPPED so the
setting can be fixed in the Config tab. With the web UI off it stops with the error.

Every 100 ms while RUNNING:

```
engine.py   get current (an interface or simulated)
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

## UART

Pack data goes out as ASCII text lines on a serial port, and the current can come in the
same way. Settings are under `interfaces.uart` in `config.yaml`:

| Setting | Default | Meaning |
|---|---|---|
| `enabled` | `false` | switch the interface on |
| `port` | `/dev/ttyUSB0` | a USB serial adapter; `/dev/serial0` for the Pi's own UART (GPIO 14 and 15) |
| `baudrate` | `115200` | 8 data bits, no parity, 1 stop bit |
| `cycle_ms` | `1000` | how often the lines are sent, a multiple of 100 |
| `modules` | `false` | `true` also sends one `$MOD` line per module |

Every line looks like `$TYPE,field,field*CS` and ends in `\r\n`. `CS` is the checksum:
the XOR of every character between `$` and `*` (not including them), written as two
upper case hex digits. Received checksums may be upper or lower case.

Sent every `cycle_ms`, in this order:

| Line | Fields |
|---|---|
| `$PACK` | pack voltage (V, 2 decimals), pack current (A, 1 decimal, discharge positive), SOC (%, 2 decimals), state name (IDLE, CHARGING, DISCHARGING, FULL, EMPTY), current timeout (0/1), temperature out of range (0/1) |
| `$CELL` | lowest cell voltage (V, 3 decimals), its module, its cell, highest cell voltage (V, 3 decimals), its module, its cell |
| `$LIM` | max charge current (A, 1 decimal), max discharge current (A, 1 decimal) |
| `$MOD` | module number, then every cell voltage of that module in whole mV. One line per module, numbered string by string like the CAN module frames. Only if `modules` is true |

Each cycle ends with one empty line (`\r\n` alone), so a serial terminal shows every
cycle as its own block. Receivers should skip empty lines.

Received:

| Line | Fields |
|---|---|
| `$CUR` | current in A, discharge positive, a number like `50`, `-20` or `12.5`. Used when `current.source` is `uart`, with the timeout like every interface (REQ-10) |

Examples:

```
$PACK,312.45,50.0,80.00,DISCHARGING,0,0*7A
$CELL,3.287,5,7,3.301,12,2*3A
$LIM,200.0,200.0*48
$MOD,1,3295,3297,3301,3290,3296,3298,3293,3300,3294,3299,3292,3296*5C

$CUR,50*6D
$CUR,-20*47
```

Rules for received lines:

- A line with a wrong checksum, a wrong format (no `$`, no `*CS`, an unknown type, a
  `$CUR` without exactly one number) or longer than 128 characters (without `\r\n`) is
  ignored, with one warning per kind of problem. The emulator never stops because of
  input.
- A line split across reads is put back together. If more than 128 characters arrive
  without an end, they are thrown away up to the next `\n`.
- Empty lines are skipped without a warning.
- The emulator's own line types (`$PACK`, `$CELL`, `$LIM`, `$MOD`) are ignored without a
  warning, so TX wired back to RX works.
- 128 characters is a limit on received lines only. A `$MOD` line for a module with more
  than about 22 cells is longer.

Sending never stops the emulator. If a write or read fails (for example a USB adapter is
unplugged), the port is closed, one warning is printed and the port is reopened every 2
seconds; once a line is written again, one more message says sending works again. A port
that cannot be opened at Play is an error naming `interfaces.uart.port`.

**Line budget.** At Play the emulator works out the most bytes one cycle can send with
this pack: every line built with the longest values the pack can give (pack voltage at
`v_max` on every cell, the largest limit as a charge current, SOC 100.00, DISCHARGING,
the highest module and cell numbers), plus one `$MOD` line per module if `modules` is
true, each with its `\r\n`, plus the empty line that ends the cycle. On the wire one byte is 10 bits (start, 8 data, stop). If
these bytes would use more than 80 % of what `baudrate` carries in one `cycle_ms`, Play
stops with an error naming `interfaces.uart.baudrate`. Example, the default 96s2p pack
(16 modules of 12 cells) at `cycle_ms: 1000`: with `modules: true` one cycle is up to
1247 bytes, so 12470 bit/s, which needs at least 15588 baud: 19200 and up work, 9600 does
not. With `modules: false` it is 104 bytes, so even 2400 baud works.

pyserial's `loop://` port (what is written can be read back) is accepted as `port`; the
tests use it. It holds only 4096 bytes, so use it for tests, not for long runs.

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
- **REQ-08** Current is simulated or comes from an interface (CAN or UART), as set by
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

UART
- **REQ-38** The UART interface in `interfaces/uart/` follows the interface contract and
  its `start()` returns True. Its settings are under `interfaces.uart` in `config.yaml`:
  `enabled` (false in the shipped file), `port` (default `/dev/serial0`), `baudrate`
  (default 115200), `cycle_ms` (default 1000, a multiple of 100) and `modules` (default
  false). A bad setting is an error naming it; a port that cannot be opened at start is
  an error naming `interfaces.uart.port`.
- **REQ-39** Lines are ASCII text `$TYPE,field,...*CS` ending in `\r\n`, CS being the
  XOR of every character between `$` and `*` as two upper case hex digits. `$PACK`,
  `$CELL` and `$LIM` are sent every `cycle_ms` with the fields in the UART section, and
  one `$MOD` line per module only when `modules` is true. Each cycle ends with one empty
  line.
- **REQ-40** A `$CUR` line sets the current when `current.source` is `uart`. A line with a
  wrong checksum, a wrong format or longer than 128 characters is ignored with a one time
  warning; the emulator never stops because of input. A line split across reads is put
  back together.
- **REQ-41** A write error never stops the emulator: one warning when it starts failing
  and one when it works again. If the port disappears (USB adapter unplugged), the
  interface tries to reopen it every 2 seconds.
- **REQ-42** At start, the bytes sent per cycle for this pack (the longest possible line
  of every type, `$MOD` included when `modules` is true, and the empty line that ends
  the cycle, 10 bits per byte) must use at
  most 80 % of the line in one `cycle_ms`. Otherwise the start stops with an error naming
  `interfaces.uart.baudrate`.

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
  and the page says so. `webui.host` and `webui.port` can be saved from the page but take
  effect only after a restart of the program; the page says so.
- **REQ-34** Only the engine thread changes the pack. The web server only reads the
  latest result and puts commands in a queue; the engine applies queued commands at the
  start of the next tick. The web UI gets a handle with two functions (read the state,
  queue a command), never the Engine itself.
- **REQ-35** The page works without internet: no external scripts, styles or fonts.
- **REQ-16** `config.yaml` is the only place settings are stored. The web UI loads its
  settings form from the file. Settings are edited only while STOPPED. Save is a queued
  command: the engine checks the values with exactly the same code it uses at Play (not a
  copy) and refuses with the same message. Then it writes the file atomically (a
  temporary file in the same folder, then `os.replace`), keeping comments and key order
  (ruamel.yaml round trip). Applying the saved settings means pressing Play.
- **REQ-17** The page remembers which version of `config.yaml` it loaded (a hash of the
  file). Save is refused if the file changed since, and the page offers to reload. While
  open, the page notices a changed file and shows a reload notice, so it never shows
  stale values.
- **REQ-18** The web UI builds its choices from what exists: batteries from the folders in
  `batteries/`, interface sections and their settings shown generically from
  `config.yaml`. A new battery or interface section appears in the Config tab without
  changes to `webui/` code.
- **REQ-36** The emulator is STOPPED (no ticks, interfaces stopped so no CAN frames,
  settings editable) or RUNNING (ticks and frames, settings locked), see Run states. The
  engine loop and the web server keep running while STOPPED. Play builds the pack and
  starts the interfaces from the saved `config.yaml`; if that fails, the emulator stays
  STOPPED and the page shows the error. Stop stops the interfaces. The page has one big
  Play or Stop button and shows the run state on every tab.
- **REQ-37** `start.autostart` (default true) starts the engine RUNNING, so the Pi
  service puts the battery on the bus at boot. With `autostart: false` it starts
  STOPPED.
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
- **REQ-31** The emulator starts at boot as a normal user (never root), after the bus step
  has finished, and restarts automatically if it stops with an error. A failed bus step
  does not block it: Play then fails, and with the web UI on the emulator stays STOPPED
  and the page shows the error.
- **REQ-32** One install script sets up both services on a Pi, and can also remove them.
- **REQ-43** A quick install script sets up a fresh Raspberry Pi OS Lite in one run:
  installs git and can-utils, installs uv if missing, uses the clone it is run from or
  clones the repository into `~/open-battery-emulator`, runs `uv sync` and the install
  script. It is safe to run twice, stops at the first error with a message naming the
  step, and prints the web UI address with the Pi's hostname at the end.
- **REQ-44** The shipped `config.yaml` turns on nothing that needs hardware: current is
  simulated, CAN and UART are disabled, the web UI is on. A fresh install runs without
  errors.
- **REQ-33** The engine stops cleanly on SIGTERM as well as Ctrl+C, calling `stop()` on
  every interface.

## UI style

The web UI follows a few rules, so it stays calm and readable:

- Calm and minimal, very little decoration: system fonts, light grey page, white cards
  with rounded corners, generous spacing, dark grey text and lighter grey secondary text.
  Dark mode follows the system setting.
- One blue accent, for controls and the selected tab.
- Colour only where something needs attention: green dot while running, green for
  charging, red for EMPTY, FULL, the temperature flag, errors and a lost connection, amber
  for the timeout flag.
- The header has a large, bold title on the left and the connection indicator on the
  right: an outline pill, green while connected, red for no connection.
- A small dot beats like a heart (two quick beats, then a pause) on the connection
  indicator while connected and on the run button while running. Nothing moves when
  stopped or disconnected.
- Messages appear next to the button that caused them, so the page never shifts: under
  the run button on the Live tab, and on the Config tab in a slot of fixed height inside
  the Save card, above the Save button, so the button never moves. Errors in red until
  closed, confirmations short and fading out, at most the newest three at a time.
- Two tabs, Live and Config, each fits on one laptop screen without scrolling. A pack too
  large for the screen scrolls inside the cell grid card. On a phone the page scrolls.
- Live tab, left: a SOC card with the big SOC value and a small time to empty or full, a
  bar (green above 50 %, amber from 20 to 50 %, red below 20 %) with a soft light moving
  the way the charge goes, and one row of symbols and numbers (lowest and highest cell with
  their position, spread, charge and discharge limit), each explained on hover or tap.
  Voltage, current and state sit stacked next to it at the same height, one line each.
- Live tab, right: the controls (sections with room and a thin line between them), the
  run time, and one run button in two equal halves that read as one unit: the state on
  the left (dot and Running or Stopped), a filled action on the right that says what
  pressing it does (green Play, red Stop).
- Simulated current: a Discharge or Charge choice and a positive number of amps, sent
  after a short pause.
- Cell grid: strings side by side, each a block with its label and one row of small
  rounded squares per module. The square size follows the card's width up to a maximum,
  and the card is only as tall as its grid and legend; the squares shrink only when the
  screen is too short, and below a minimum the grid scrolls. Normal cells are a calm light
  green, amber for a cell more than 50 mV from the pack average, red for a cell at or
  beyond its minimum or maximum voltage, and for the cell that caused FULL (highest) or
  EMPTY (lowest). The lowest and highest cell get a thin outline. A short legend explains
  this. A cell shows its module, cell number, voltage and SOC on hover or tap.
- Config tab: the setting groups as cards in an even grid, cards in one row the same
  height. Interfaces spans two columns at the bottom; next to it a Save card with a short
  reminder, room for messages, the number of unsaved changes and the Save button.
- Flags and the FULL and EMPTY states explain themselves on hover or tap. A current source
  that cannot be chosen right now is shown greyed with the reason.

## How we work (light V-model)

Every requirement on the left has a check on the right:

| Define | Check |
|---|---|
| Idea | Runs on the Pi with virtual CAN and a physical UART link (v1.0) |
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
run by hand, following README "Quick install (Raspberry Pi)", and recorded here. The unit file
contents are also checked automatically in `tests/test_deploy.py`.

| REQ | Steps | Expected | Date | Result |
|---|---|---|---|---|
| REQ-32 | `sudo deploy/install.sh` | both services installed, enabled and started, no errors |19.11.2026 |Pass |
| REQ-31 | `systemctl status open-battery-emulator` | active (running), as the normal user (not root); `journalctl -u open-battery-emulator` shows a status line every 10 s |19.11.2026 |Pass |
| REQ-31 | `sudo reboot`, log in, `systemctl status open-battery-emulator` | running again after the reboot, `candump vcan0` shows frames |19.11.2026 |Pass |
| REQ-31 | `sudo systemctl kill -s KILL open-battery-emulator`, wait, `systemctl status open-battery-emulator` | running again within about 5 s, with a new PID |19.11.2026 |Pass |
| REQ-32 | `sudo deploy/install.sh --remove` | both services stopped and disabled, unit files gone from `/etc/systemd/system` |19.11.2026 |Pass |
| REQ-15 | `webui.enabled: true`, start the emulator, open `http://localhost:8080` on the laptop | values update about twice a second, the cell grid shows every module, Set SOC / Hold SOC / simulated current change the values on the next update, SOC 150 shows an error |25.11.2026 |Pass |
| REQ-15 | on the Pi, open `http://<hostname>.local:8080` on a phone on the same network (`<hostname>` is the name set in Raspberry Pi Imager) | page readable without zooming, controls usable, tapping a cell shows its voltage | | |
| REQ-39, REQ-40 | laptop: `socat -d -d pty,raw,echo=0,link=/tmp/ttyOBE0 pty,raw,echo=0,link=/tmp/ttyOBE1`, `interfaces.uart` enabled with `port: /tmp/ttyOBE0` and `current.source: uart`, start the emulator, `picocom -b 115200 /tmp/ttyOBE1`, then `printf '$CUR,50*6D\r\n' > /tmp/ttyOBE1` once a second | `$PACK`, `$CELL` and `$LIM` once a second in picocom; the status line shows 50 A while the `$CUR` lines come, then TIMEOUT and 0 A within 0.5 s after they stop |26.11.2026 |Pass |
| REQ-39, REQ-40, REQ-41 | Pi: UART enabled with `raspi-config`, user in `dialout`, `port: /dev/serial0`, `current.source: uart`. CP2102 adapter on the laptop, wired Pi TX (GPIO 14, pin 8) to adapter RX, Pi RX (GPIO 15, pin 10) to adapter TX, GND (pin 6) to GND. On the laptop `picocom -b 115200 /dev/ttyUSB0` and send `$CUR,50*6D` lines; unplug the adapter for a few seconds and plug it back | lines arrive on the laptop, the Pi's status line shows 50 A; unplugging does not stop the emulator, lines come back within about 2 s of plugging in | | |
| REQ-36 | with CAN on `vcan0` and `candump vcan0` running, press Stop, then Play | Stop: frames stop, run button shows a grey dot and Stopped, Config fields editable. Play: frames again, run button shows a green dot and Running, fields locked |25.11.2026 |Pass |
| REQ-16 | STOPPED, Config tab: change cells per module, Save, Play | "Saved" message, Live tab shows the new grid, `config.yaml` keeps its comments; a value like cells per module 0 shows the error and the file is unchanged |25.11.2026 |Pass |
| REQ-17 | Config tab open, edit `config.yaml` in an editor and save it | within a few seconds a reload notice appears in the Save card; Reload shows the new value |25.11.2026 |Pass |
| REQ-43, REQ-44 | fresh Raspberry Pi OS Lite: quick install with `deploy/bootstrap.sh`, then `sudo reboot`, then open the printed web UI address from a phone | every step finishes, the address is printed; after the reboot the web UI opens on the phone, RUNNING, no errors shown | | |

## Milestones

| # | Goal | Done when |
|---|---|---|
| M0 | Setup | skeleton and green CI on `main` |
| M1 | Cell and pack | `cell.py` and `pack.py` pass their tests; 100 A for 1 h on a 100 Ah cell takes SOC from 100 % to 0 %; every state transition, cell variation, Set SOC and Hold SOC tested |
| M2 | Engine | engine runs in the terminal from `config.yaml` with simulated current and prints values |
| M3 | CAN | M3a: pack messages on `vcan0` decode with the DBC; module frames follow the pattern. M3b: 0x180 changes the current; timeout works |
| M4 | Pi | runs on the Pi as a service on boot with `vcan0`, survives a reboot, restarts after a crash |
| M5 | Web UI | M5a: optional page shows live values and changes the simulated current. M5b: Live and Config tabs, Play / Stop, settings saved to `config.yaml` (REQ-16, 17, 18, 36, 37). M5c: edits CAN message IDs and module frame IDs with checks before saving (REQ-29) |
| M6 | UART interface | frames seen and current set over a virtual serial pair (socat) on the laptop, and over the Pi's UART with a CP2102 adapter on the laptop (REQ-38 to REQ-42) |
| v1.0 | Release | runs on the Pi with virtual CAN and a physical UART link, web UI checked from a phone, every REQ is verified (a passing test, or a manual check recorded in PROJECT.md), docs complete, tagged release |

## Later

Temperature model (temperature changing with current), fault injection, NMC and other
batteries, gradual hysteresis transition (OCV moves from one curve to the other over a
few % SOC instead of switching at once), per module capacity, load profiles from CSV, script that writes module frames
into a DBC for decoding tools, physical CAN test (MCP2515 HAT on the Pi and a CANable on a laptop, frames received and current sent over real wires), isoSPI interface (needs an LTC6820 type
transceiver and tight timing, likely on an MCU rather than Linux), Modbus or MQTT for BESS, cell and module signals in the DBC by naming pattern (for
example `Cell_M3_C7`, `ModuleTemp_M3`), filled by module and cell number, an optional
`check(settings, pack_info)` function in the interface contract, so Save can catch
interface errors like a broken DBC before Play.
