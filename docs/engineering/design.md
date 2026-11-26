# Design

How the emulator is built: the three levels of the model, the tick, the run states and
state machine, and the contracts that keep interfaces and the web UI apart from the model.

## The three levels

Three levels, each only uses the one below it:

| Level | File | Knows about | Does |
|---|---|---|---|
| High | `config.py` | `config.yaml` | reads and checks it, builds the settings form and battery list for the web UI, saves it. Only `engine.py` uses it |
| High | `engine.py` | current source, time, run state | Play and Stop, picks the current (an interface or simulated), runs the loop, hands results to interfaces |
| Mid | `pack.py` | many cells | builds strings and modules, splits current, finds min and max cell, runs the state machine |
| Low | `cell.py` | one cell | reads the battery CSVs, OCV lookup, SOC by coulomb counting, cell voltage, current limits |

??? info "Repository layout"
    ```
    open-battery-emulator/
    ├── README.md          how to run it
    ├── mkdocs.yml         settings of the documentation site
    ├── docs/              this documentation, Markdown pages and images
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

    Add a battery: copy a folder in `batteries/`. Add an interface (isoSPI, Modbus...): add a folder in `interfaces/` and a section under `interfaces:` in `config.yaml`. Neither needs a change in `emulator/`, and the web UI shows both automatically.

## The tick

A tick is one pass of the loop, 100 ms of emulated time. Every 100 ms while RUNNING:

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

## Run states

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

## State machine

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

## Test controls

The engine offers Set SOC and Hold SOC for testing (REQ-21); what they do is in
[Using it](../using.md#live-tab).

## Contracts

The engine talks to interfaces and to the web UI only through these functions, so
`emulator/` never imports anything CAN, UART or web specific.

??? info "Interface contract"
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

??? info "Web UI contract and threading"
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

    The web server runs in its own background thread inside the same program. Only the engine thread changes the pack: the web thread reads the latest result and queues commands, which the engine applies at the start of the next tick, so the two never change the same data at the same time (REQ-34).

## UI style

??? info "UI style rules"
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
