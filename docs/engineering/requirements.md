# Requirements

Every feature starts as a requirement here. IDs never change and are never reused, so a
test or check can name the requirement it proves; grouped by topic, so not in order.
How each is checked: [Verification](verification.md).

## Settings

| ID | Requirement |
|---|---|
| **REQ-01** | All settings are in `config.yaml`. The emulator runs from it alone. |

## Battery

| ID | Requirement |
|---|---|
| **REQ-02** | A battery type is a folder in `batteries/` with `cell.csv` and four tables: `ocv_charge.csv`, `ocv_discharge.csv`, `limit_charge.csv`, `limit_discharge.csv`, each with SOC in % as rows and temperatures as columns. Any number of rows and columns is allowed. A new battery, an extra row or an extra column needs no code change. |
| **REQ-23** | Table values between rows and columns are found by linear interpolation in SOC and temperature. Outside the SOC rows the nearest row is used. A table that is not in ascending order is rejected at start with a clear error naming the file. |

## Pack

| ID | Requirement |
|---|---|
| **REQ-03** | Pack = P strings in parallel, each of S modules in series, each of C cells in series. |
| **REQ-04** | Discharge current is positive. SOC is in percent everywhere (files, config, code). It changes by coulomb counting and stays between 0 and 100 %. |
| **REQ-05** | Cell voltage = OCV(SOC, temperature) - I x R0, using the charge or discharge OCV table by current direction as described in Battery files. The OCV curve of every cell follows the direction of the pack current; at 0 A each cell keeps its last curve, so circulating currents between strings never flip curves. Current splits between parallel strings by resistance. All strings share one pack voltage: V_pack = (Σ OCV_k/R_k − I_pack) / (Σ 1/R_k), and I_k = (OCV_k − V_pack) / R_k, where OCV_k and R_k are the sums over the string's cells at the start of the tick. If any string's R_k is 0, current splits equally. |
| **REQ-06** | Allowed charge and discharge current per cell come from the limit tables at the cell's SOC and the set temperature, interpolated in both. The pack charge and discharge limits are P times the lowest cell limit; the temperature flag is set if any cell is out of range. |
| **REQ-22** | When the limit tables have two or more temperature columns, the first and last define the operating range. Outside it, both current limits are 0 and a temperature flag is set. |
| **REQ-20** | Each cell gets its own capacity, R0 and start SOC, spread randomly around the battery file values by the percentages in `variation`. Capacity and R0 spread by a percent of the file value, SOC by SOC percentage points. The same seed gives the same pack every run. All percentages 0 gives identical cells. |

## State

| ID | Requirement |
|---|---|
| **REQ-07** | The emulator has the states IDLE, CHARGING, DISCHARGING, FULL and EMPTY with the transitions in the state table. FULL blocks charging and is left only by a discharge current; EMPTY blocks discharging and is left only by a charge current. |
| **REQ-21** | The engine offers Set SOC (jump all cells to a SOC, keeping the spread) and Hold SOC (freeze SOC while current flows). The web UI shows both as controls. |

## Current input

| ID | Requirement |
|---|---|
| **REQ-08** | Current is simulated or comes from an interface (CAN or UART), as set by `current.source` in `config.yaml`: `simulated` or the name of an enabled interface whose `start()` says it can provide current commands. Anything else stops the start with an error naming `current.source`. |
| **REQ-09** | Simulated current behaves exactly like a current command arriving every 100 ms. |
| **REQ-10** | If the source is an interface and no current command arrives within `timeout_ms`, current is 0 and the timeout flag is set. The timeout is handled in `engine.py` for any interface, never in interface code. The last command is held until more than `timeout_ms` has passed since it arrived. At start no command has arrived, so the emulator starts in timeout (0 A, flag set). The flag clears as soon as a command arrives. |
| **REQ-24** | Pack currents within ±`current.deadband_a` count as 0. The deadband is applied once per tick in `pack.py` before anything else, and only to the pack current, never to string currents. |

## CAN

| ID | Requirement |
|---|---|
| **REQ-11** | Driver (`socketcan` or `virtual`), channel, bitrate and DBC file are set under `interfaces.can` in `config.yaml`. |
| **REQ-12** | Pack messages are built from the DBC file using the signal names above. Changing IDs, layout, scaling or cycle time needs only a DBC edit. |
| **REQ-13** | Module frames follow the pattern set in `config.yaml`. |
| **REQ-27** | A value outside its CAN signal range (DBC signals and module frame fields) is clamped to the range and a warning is printed once per signal. A received frame that cannot be decoded is skipped with a warning. The emulator never stops because of a value. |
| **REQ-28** | If the bus refuses a frame (for example no other device acknowledges on `can0`), the emulator keeps running. It prints one warning when sending starts failing and one when it works again. |

## UART

| ID | Requirement |
|---|---|
| **REQ-38** | The UART interface in `interfaces/uart/` follows the interface contract and its `start()` returns True. Its settings are under `interfaces.uart` in `config.yaml`: `enabled` (false in the shipped file), `port` (default `/dev/ttyUSB0`), `baudrate` (default 115200), `cycle_ms` (default 1000, a multiple of 100) and `modules` (default false). A bad setting is an error naming it; a port that cannot be opened at start is an error naming `interfaces.uart.port`. |
| **REQ-39** | Lines are ASCII text `$TYPE,field,...*CS` ending in `\r\n`, CS being the XOR of every character between `$` and `*` as two upper case hex digits. `$PACK`, `$CELL` and `$LIM` are sent every `cycle_ms` with the fields in the UART section, and one `$MOD` line per module only when `modules` is true. Each cycle ends with one empty line. |
| **REQ-40** | A `$CUR` line sets the current when `current.source` is `uart`. A line with a wrong checksum, a wrong format or longer than 128 characters is ignored with a one time warning; the emulator never stops because of input. A line split across reads is put back together. |
| **REQ-41** | A write error never stops the emulator: one warning when it starts failing and one when it works again. If the port disappears (USB adapter unplugged), the interface tries to reopen it every 2 seconds. |
| **REQ-42** | At start, the bytes sent per cycle for this pack (the longest possible line of every type, `$MOD` included when `modules` is true, and the empty line that ends the cycle, 10 bits per byte) must use at most 80 % of the line in one `cycle_ms`. Otherwise the start stops with an error naming `interfaces.uart.baudrate`. |

## Structure

| ID | Requirement |
|---|---|
| **REQ-14** | Every interface follows the interface contract (`start`, `send`, `read_current`, `stop`) and is switched on in `config.yaml`. The engine loads it by name with `importlib` and never imports anything interface specific. Adding one needs no change in `emulator/`. The web UI only reads the result and queues commands (REQ-34). |
| **REQ-26** | When any interface is enabled, `--speed` must be 1, because devices on the bus expect real time cycle times. Otherwise the start stops with an error naming `--speed`. |

## Terminal

| ID | Requirement |
|---|---|
| **REQ-25** | When run from the command line, the engine prints one status line per second of emulated time with elapsed time, state, pack current, voltage, average SOC, and lowest and highest cell with module and cell number. It shows `TIMEOUT` when the timeout flag is set. `--status-every N` prints the status line every N seconds of emulated time (default 1). |

## Web UI

| ID | Requirement |
|---|---|
| **REQ-15** | The web UI is optional. When enabled it shows current, SOC, voltages, state and limits live, and can change the source and the simulated current. When `webui.enabled` is true, the engine starts a small web server in a background thread in the same process. When false, nothing about the engine changes. `webui.host` (default `0.0.0.0`) and `webui.port` (default 8080) come from `config.yaml`. Live changes from the page (source, simulated current, Set SOC, Hold SOC) are not saved to `config.yaml`, and the page says so. `webui.host` and `webui.port` can be saved from the page but take effect only after a restart of the program; the page says so. |
| **REQ-34** | Only the engine thread changes the pack. The web server only reads the latest result and puts commands in a queue; the engine applies queued commands at the start of the next tick. The web UI gets a handle with two functions (read the state, queue a command), never the Engine itself. |
| **REQ-35** | The page works without internet: no external scripts, styles or fonts. |
| **REQ-16** | `config.yaml` is the only place settings are stored. The web UI loads its settings form from the file. Settings are edited only while STOPPED. Save is a queued command: the engine checks the values with exactly the same code it uses at Play (not a copy) and refuses with the same message. Then it writes the file atomically (a temporary file in the same folder, then `os.replace`), keeping comments and key order (ruamel.yaml round trip). Applying the saved settings means pressing Play. |
| **REQ-17** | The page remembers which version of `config.yaml` it loaded (a hash of the file). Save is refused if the file changed since, and the page offers to reload. While open, the page notices a changed file and shows a reload notice, so it never shows stale values. |
| **REQ-18** | The web UI builds its choices from what exists: batteries from the folders in `batteries/`, interface sections and their settings shown generically from `config.yaml`. A new battery or interface section appears in the Config tab without changes to `webui/` code. |
| **REQ-36** | The emulator is STOPPED (no ticks, interfaces stopped so no CAN frames, settings editable) or RUNNING (ticks and frames, settings locked), see Run states. The engine loop and the web server keep running while STOPPED. Play builds the pack and starts the interfaces from the saved `config.yaml`; if that fails, the emulator stays STOPPED and the page shows the error. Stop stops the interfaces. The page has one big Play or Stop button and shows the run state on every tab. |
| **REQ-37** | `start.autostart` (default true) starts the engine RUNNING, so the Pi service puts the battery on the bus at boot. With `autostart: false` it starts STOPPED. |

## Platform

| ID | Requirement |
|---|---|
| **REQ-19** | Runs on Linux with SocketCAN. Verified with `vcan0` on a laptop and on the Pi; physical CAN adapters (`can0`) are a Later item in [Milestones and Later](milestones.md). |

## Deployment

| ID | Requirement |
|---|---|
| **REQ-30** | The CAN bus is brought up at boot by a separate step run as root, reading channel and bitrate from `config.yaml`. For a channel starting with `vcan` it loads the vcan module and creates the device; otherwise it sets the bitrate with `restart-ms 100`, so the controller recovers from bus off automatically, and brings the device up. |
| **REQ-31** | The emulator starts at boot as a normal user (never root), after the bus step has finished, and restarts automatically if it stops with an error. A failed bus step does not block it: Play then fails, and with the web UI on the emulator stays STOPPED and the page shows the error. |
| **REQ-32** | One install script sets up both services on a Pi, and can also remove them. |
| **REQ-43** | A quick install script sets up a fresh Raspberry Pi OS Lite in one run: installs git and can-utils, installs uv if missing, uses the clone it is run from or clones the repository into `~/open-battery-emulator`, runs `uv sync` and the install script. It is safe to run twice, stops at the first error with a message naming the step, and prints the web UI address with the Pi's hostname at the end. |
| **REQ-44** | The shipped `config.yaml` turns on nothing that needs hardware: current is simulated, CAN and UART are disabled, the web UI is on. A fresh install runs without errors. |
| **REQ-33** | The engine stops cleanly on SIGTERM as well as Ctrl+C, calling `stop()` on every interface. |

## Verification

| ID | Requirement |
|---|---|
| **REQ-45** | Every requirement is verified by at least one test whose docstring names it (`Checks REQ-xx`) or by a row in the manual checks table on [Verification](verification.md). A requirement with neither, or a test or manual check naming an ID that does not exist, fails CI. A manual check without a result yet (pending) and a deferred requirement do not fail CI. The Verification page shows a traceability table generated during the docs build. |

## Deferred

Requirements written down but not built yet. They keep their IDs, are not checked by CI,
and show as deferred in the traceability table on [Verification](verification.md).

| ID | Requirement |
|---|---|
| **REQ-29** | The web UI lets the user change the CAN ID of each message in the DBC (PackStatus, CellStats, PackLimits, CurrentCommand) and the module frame `base_id` and `id_step` in `config.yaml`. Signal names and layouts are not editable in the UI. Save writes the IDs into the DBC file and `config.yaml`. Save is refused with a clear message if two messages share an ID, a pack message falls inside the module frame range, an ID is above 0x7FF, or the module range does not fit the pack. The UI shows the module ID range for the current pack size and notes that lower IDs have higher priority on the bus. |
