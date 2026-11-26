# Configuration

Every setting in `config.yaml`, and the battery files. Edit the file in a text editor or
in the web UI [Config tab](using.md#config-tab).

`config.yaml` is YAML: `name: value` pairs, indented by two spaces to show their group,
`#` starts a comment. The shipped file turns on nothing that needs hardware (current
simulated, CAN and UART off, web UI on), so a fresh install runs without errors. Every
setting is checked at Play and Save; a bad value gives an error naming it, for example
`pack.cells_per_module must be at least 1`.

**Restart column:**

| Says | Do this |
|---|---|
| Play | Stop, Save, Play |
| Program | restart the program (Ctrl+C and start again, or `sudo systemctl restart open-battery-emulator` on the Pi) |
| CAN service | on the Pi also run once (or reboot): `sudo systemctl restart open-battery-emulator-can open-battery-emulator` |

??? info "The whole shipped config.yaml"
    ```yaml
    --8<-- "config.yaml"
    ```

## Battery and pack

The pack is P strings in parallel, each of S modules in series, each of C cells in series.
The default 12 x 8 x 2 is 192 cells, 96 in series ("96s2p").

| Setting | What it means | Default | Allowed | Restart |
|---|---|---|---|---|
| `battery` | battery type: a folder in `batteries/` | `lfp_100ah` | any folder name in `batteries/` | Play |
| `pack.cells_per_module` | cells in series per module (C) | `12` | whole number ≥ 1 | Play |
| `pack.modules_in_series` | modules in series per string (S) | `8` | whole number ≥ 1 | Play |
| `pack.strings_in_parallel` | strings side by side (P): more capacity and current, same voltage | `2` | whole number ≥ 1 | Play |

## start

| Setting | What it means | Default | Allowed | Restart |
|---|---|---|---|---|
| `soc` | SOC of every cell at Play, % | `80` | 0 to 100 | Play |
| `temperature_c` | pack temperature, °C, fixed for now. Outside the battery's operating range the limits are 0 and the temperature flag is set | `25` | any number | Play |
| `autostart` | `true` presses Play at program start; `false` starts Stopped | `true` (also if missing) | `true`, `false` | Program |

## variation

Real cells differ. Each cell gets its own capacity, R0 (internal resistance, which makes
the voltage drop under load) and start SOC, spread randomly around the battery file.

| Setting | What it means | Default | Allowed | Restart |
|---|---|---|---|---|
| `capacity_pct` | capacity within ± this % of the file | `2` | number ≥ 0 | Play |
| `r0_pct` | R0 within ± this % of the file | `5` | number ≥ 0 | Play |
| `soc_pct` | start SOC within ± this many SOC points | `1` | number ≥ 0 | Play |
| `seed` | same seed = same pack every run | `42` | any whole number | Play |

All three percentages `0` gives identical cells.

## current

Amps, **discharge positive**: `50` is 50 A out of the battery, `-20` is 20 A in.

| Setting | What it means | Default | Allowed | Restart |
|---|---|---|---|---|
| `source` | where the current comes from ([simulated or a device](interfaces/index.md)) | `simulated` | `simulated`, or an enabled interface that can receive current (`can`, `uart`) | Play, or live in the web UI |
| `simulated_a` | current when `source` is `simulated` | `50` | any number | Play, or live in the web UI |
| `timeout_ms` | from a device: no command within this time means 0 A and the timeout flag | `500` | number > 0 | Play |
| `deadband_a` | pack currents within ± this count as 0 | `0.5` | number ≥ 0 | Play |

## interfaces.can

Details of the messages are in [CAN](interfaces/can.md).

| Setting | What it means | Default | Allowed | Restart |
|---|---|---|---|---|
| `enabled` | send pack data on CAN | `false` | `true`, `false` | Play + CAN service |
| `driver` | `socketcan`: a Linux CAN device, real or virtual. `virtual`: python-can's in-memory bus, tests and CI only | `socketcan` | `socketcan`, `virtual` | Play |
| `channel` | CAN device name | `can0` | e.g. `can0`, `vcan0` | Play + CAN service |
| `bitrate` | bus speed, bit/s, same on every device; ignored by `vcan` | `500000` | whole number ≥ 1, e.g. `250000`, `500000` | Play + CAN service |
| `dbc` | the DBC file defining the messages, path relative to the project folder works | `interfaces/can/battery.dbc` | path to a DBC file | Play |
| `module_frames.base_id` | CAN ID of module 1's first frame (`0x200` = 512) | `0x200` | whole number; highest module ID ≤ `0x7FF`, no clash with DBC IDs | Play |
| `module_frames.id_step` | IDs per module | `0x10` | ≥ `0x10` | Play |
| `module_frames.cells_per_frame` | cell voltages per frame | `4` | 1 to 4, a module's frames must fit below offset `0xF` | Play |
| `module_frames.cycle_ms` | how often module frames are sent, ms | `1000` | multiple of 100, ≥ 100 | Play |

## interfaces.uart

Details of the lines are in [UART](interfaces/uart.md).

| Setting | What it means | Default | Allowed | Restart |
|---|---|---|---|---|
| `enabled` | send pack data as text lines | `false` | `true`, `false` | Play |
| `port` | serial port | `/dev/ttyUSB0` | e.g. `/dev/ttyUSB0` (USB adapter), `/dev/serial0` (Pi's UART), `/tmp/ttyOBE0` (virtual), `loop://` (tests) | Play |
| `baudrate` | speed, bit/s, 8 data bits, no parity, 1 stop bit; same on both ends | `115200` | whole number ≥ 1, fast enough for the [line budget](interfaces/uart.md#line-budget) | Play |
| `cycle_ms` | how often the lines are sent, ms | `1000` | multiple of 100, ≥ 100 | Play |
| `modules` | also send one `$MOD` line per module | `false` | `true`, `false` | Play |

## webui

| Setting | What it means | Default | Allowed | Restart |
|---|---|---|---|---|
| `enabled` | start the web page (no `webui` section also means off) | `true` | `true`, `false` | Program |
| `host` | `0.0.0.0` = reachable from other devices, `127.0.0.1` = this computer only | `0.0.0.0` | an address | Program |
| `port` | port in the address `http://localhost:8080` | `8080` | 0 to 65535 (0 = any free port, tests) | Program |

## Battery files

A battery type is a folder in `batteries/` with five CSV files (plain text tables that a
spreadsheet can open). Values are per cell, where a "cell" is one series element: for
cells in parallel inside a module, enter the combined values. The `lfp_100ah` values are a
generic placeholder, to be replaced with real data.

**Add a battery:**

1. `cp -r batteries/lfp_100ah batteries/my_cell`
2. Edit the five files with your cell's data.
3. Set `battery: my_cell` (or pick it in the Config tab, which lists every folder).
4. Press Play. No code change is needed.

| File | Holds |
|---|---|
| `cell.csv` | `capacity` (Ah), `r0` (ohm), `v_min` (V, reaching it makes the pack EMPTY), `v_max` (V, reaching it makes it FULL) |
| `ocv_charge.csv`, `ocv_discharge.csv` | OCV (open circuit voltage: the cell voltage with no current) per SOC and temperature, V |
| `limit_charge.csv`, `limit_discharge.csv` | max charge or discharge current per SOC and temperature, A per cell |

??? info "File formats and table rules"
    `cell.csv`:

    ```
    parameter,value,unit
    capacity,100,Ah
    r0,0.001,ohm
    v_min,2.50,V
    v_max,3.65,V
    ```

    The four tables share one shape: SOC in % as rows, temperature in °C as columns. You
    can give every SOC step or just a few points; values in between are linearly
    interpolated in both SOC and temperature. This minimal table is valid:

    ```
    soc,0,25,45
    0,2.80,2.80,2.81
    10,3.19,3.20,3.20
    90,3.31,3.32,3.32
    100,3.44,3.45,3.45
    ```

    `ocv_discharge.csv` (volts), same for `ocv_charge.csv`:

    ```
    soc,-20,-10,0,10,25,45,55
    0,2.791,2.793,2.795,2.797,2.800,2.804,2.806
    1,2.891,2.893,2.895,2.897,2.900,2.904,2.906
    ...
    100,3.441,3.443,3.445,3.447,3.450,3.454,3.456
    ```

    `limit_charge.csv` (amps per cell), same for `limit_discharge.csv`:

    ```
    soc,-20,-10,0,10,25,45,55
    0,0.0,0.0,0.0,30.0,100.0,100.0,30.0
    ...
    100,0.0,0.0,0.0,0.0,0.0,0.0,0.0
    ```

    | Rule | |
    |---|---|
    | Order | SOC rows and temperature columns ascending, no duplicates; otherwise Play stops with an error naming the file |
    | Outside the SOC rows | the nearest row is used |
    | One temperature column | temperature is ignored |
    | Add a temperature | add a column. The first and last column are the operating range |
    | Outside the operating range | both current limits are 0 and a temperature flag is set. OCV uses the nearest column |
    | Charge or discharge OCV | charging uses the charge OCV, discharging the discharge OCV. IDLE keeps the curve of the last current direction (discharge curve at start). This gives LFP like hysteresis |
