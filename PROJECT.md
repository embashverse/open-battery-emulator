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
│       ├── can_bus.py
│       └── battery.dbc    default message definitions, replace with your own
├── webui/             optional: live values, current, edit and save config.yaml
└── tests/             test_cell.py, test_pack.py, test_engine.py, ...
```

Add a battery: copy a folder in `batteries/`. Add an interface (UART, isoSPI, Modbus...):
add a folder in `interfaces/` and a section under `interfaces:` in `config.yaml`. Neither
needs a change in `emulator/`, and the web UI shows both automatically.

### Interface contract

Every interface folder has one Python file with the same three functions:

```
start(settings)       open the port or bus, using its section of config.yaml
send(result)          send the latest pack result (called every tick)
read_current()        latest current command, or None if this interface has no input
```

The engine calls these and nothing else, so it never needs to know what CAN or UART is.

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
  source: simulated        # simulated, or the name of an interface (can)
  simulated_a: 50          # discharge positive
  timeout_ms: 500
  deadband_a: 0.5          # pack currents within +-0.5 A count as 0

interfaces:
  can:
    enabled: true
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
your own file. The only rule: signals must use the emulator's names below, so the
emulator knows what to put where.

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

Modules are numbered string by string. Little endian.

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
  `current.source` in `config.yaml`.
- **REQ-09** Simulated current behaves exactly like a current command arriving every
  100 ms.
- **REQ-10** If the source is an interface and no current command arrives within
  `timeout_ms`, current is 0 and the timeout flag is set.
- **REQ-24** Pack currents within ±`current.deadband_a` count as 0. The deadband is
  applied once per tick in `pack.py` before anything else, and only to the pack current,
  never to string currents.

CAN
- **REQ-11** Channel, bitrate and DBC file are set under `interfaces.can` in `config.yaml`.
- **REQ-12** Pack messages are built from the DBC file using the signal names above.
  Changing IDs, layout, scaling or cycle time needs only a DBC edit.
- **REQ-13** Module frames follow the pattern set in `config.yaml`.

Structure
- **REQ-14** Every interface follows the interface contract (`start`, `send`,
  `read_current`) and is switched on in `config.yaml`. Adding one needs no change in
  `emulator/`. The web UI only reads the result and may set the simulated current.

Web UI
- **REQ-15** The web UI is optional. When enabled it shows current, SOC, voltages, state
  and limits live, and can change the source and the simulated current.
- **REQ-16** `config.yaml` is the only place settings are stored. The web UI loads its
  settings form from the file, and a Save button checks the values and writes them back
  to the file, keeping comments. The emulator then restarts with the new settings.
- **REQ-17** If `config.yaml` was edited by hand while running, the web UI shows that the
  file changed and offers to reload it, so the UI never shows stale values.
- **REQ-18** The web UI builds its choices from what exists: batteries from the folders in
  `batteries/`, interfaces and their settings from `config.yaml`. A new battery or
  interface appears in the UI without UI code changes.

Platform
- **REQ-19** Runs on Linux with SocketCAN: `vcan0` on a laptop, `can0` on the Pi.

## How we work (light V-model)

Every requirement on the left has a check on the right:

| Define | Check |
|---|---|
| Idea | Runs on the Pi with a real device on the bus (M5) |
| Requirements | Each REQ has at least one test |
| High level (`engine.py`) | `test_engine.py`: run many ticks from a config, check the whole result |
| Mid level (`pack.py`) | `test_pack.py`: topology, current split, state machine |
| Low level (`cell.py`) | `test_cell.py`: CSV loading, OCV, SOC, voltage, limits |

Rules:
1. Every feature starts as a requirement in this file.
2. Every test names the requirement it checks: `"""Checks REQ-04."""`
   See coverage with `grep -rn "REQ-" tests/`
3. One task per branch, merged by pull request. CI must be green.
4. AI writes code, a human reviews and understands it before merging.

## Milestones

| # | Goal | Done when |
|---|---|---|
| M0 | Setup | skeleton and green CI on `main` |
| M1 | Cell and pack | `cell.py` and `pack.py` pass their tests; 100 A for 1 h on a 100 Ah cell takes SOC from 100 % to 0 %; every state transition, cell variation, Set SOC and Hold SOC tested |
| M2 | Engine | engine runs in the terminal from `config.yaml` with simulated current and prints values |
| M3 | CAN | pack messages on `vcan0` decode with the DBC; module frames follow the pattern; 0x180 changes the current; timeout works |
| M4 | Pi | runs on the Pi as a service on boot with a CAN adapter; another device receives and sends |
| M5 | Web UI | optional page shows live values, changes the simulated current, and saves settings to `config.yaml` |
| v1.0 | Release | README quick start works, every REQ has a passing test |

## Later

Temperature model (temperature changing with current), fault injection, NMC and other
batteries, gradual hysteresis transition (OCV moves from one curve to the other over a
few % SOC instead of switching at once), per module capacity, load profiles from CSV, script that writes module frames
into a DBC for decoding tools, UART interface, isoSPI interface (needs an LTC6820 type
transceiver and tight timing, likely on an MCU rather than Linux), Modbus or MQTT for BESS.
