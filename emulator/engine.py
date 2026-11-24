"""High level: current source, time and run state.

Picks the current (from an interface or simulated), runs the 100 ms loop and
hands each result to the enabled interfaces. Everything about config.yaml
(reading, checking, saving) is in config.py. Uses only config.py and pack.py.

The emulator is STOPPED (no ticks, interfaces closed, settings editable) or
RUNNING (ticks and frames, settings locked). Play builds the pack and starts
the interfaces from the saved config.yaml; Stop closes the interfaces. The
loop and the web UI keep running in both states (REQ-36).

When webui.enabled is true it also starts the web UI (loaded by name, like the
interfaces) and gives it a small handle to read the state and queue commands.
Only this engine thread ever changes the pack (REQ-34).

Run it from the project folder:
    uv run python -m emulator.engine --config config.yaml --speed 1 --status-every 1

Units: SOC in percent (0 to 100), current in amps with discharge positive,
temperature in degrees C, voltage in volts, time in seconds.
"""

import argparse
import copy
import importlib
import math
import queue
import signal
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType

from emulator.config import (
    RESTART_NEEDED,
    ConfigError,
    EngineConfig,
    apply_changes,
    check_autostart,
    check_config,
    check_speed,
    check_webui,
    read_config_file,
    read_form,
    write_config_file,
)
from emulator.pack import (
    Pack,
    PackResult,
    build_pack,
    pack_info,
    set_hold_soc,
    set_soc,
    tick,
)

# --- 1. Constants -------------------------------------------------------------

TICK_S = 0.1  # emulated time per step; the speed factor never changes this
TICKS_PER_SECOND = 10  # 1 s / TICK_S, used to print the status line every N s
CONTRACT = ("start", "send", "read_current", "stop")  # interface functions
WEBUI_CONTRACT = ("start", "stop")  # web UI functions
# Run states (REQ-36).
STOPPED = "STOPPED"
RUNNING = "RUNNING"
# Commands the web UI can queue, checked in check_command(). Play, Stop and
# Save work while STOPPED (Save only then); the others need RUNNING.
RUN_COMMANDS = ("play", "stop", "save_config")
LIVE_COMMANDS = ("set_source", "set_simulated_current", "set_soc", "hold_soc")
COMMANDS = RUN_COMMANDS + LIVE_COMMANDS
ACTIONS_KEPT = 10  # results of the last commands, shown by the page


# --- 2. Data shapes -----------------------------------------------------------


class CommandError(ValueError):
    """A command from the web UI that cannot be applied. The message says why."""


@dataclass
class Engine:
    config_path: Path
    speed: float = 1.0
    run_state: str = STOPPED
    error: str | None = None  # why the last Play failed, None after a good one
    # Set by every Play from the saved config.yaml. None before the first one.
    config: EngineConfig | None = None
    pack: Pack | None = None
    interfaces: list[ModuleType] = field(default_factory=list)  # started ones
    tick_count: int = 0  # since the last Play
    last_result: PackResult | None = None
    # Live settings: they start from config.yaml at Play and the web UI can
    # change them while running. They are never written back to config.yaml.
    current_source: str = "simulated"
    simulated_a: float = 0.0
    # Current from an interface (REQ-08, REQ-10). Unused when simulated.
    current_interface: ModuleType | None = None  # the one named by current_source
    current_sources: dict[str, ModuleType] = field(default_factory=dict)  # can provide
    command_a: float = 0.0  # the last current command received
    command_tick: int | None = None  # tick_count when it arrived; None = none yet
    # Web UI (REQ-15, REQ-34). The web thread only puts into `commands` (taking
    # an id under `id_lock`) and reads `live_state`; everything else is used by
    # the engine thread only.
    commands: queue.Queue = field(default_factory=queue.Queue)
    id_lock: threading.Lock = field(default_factory=threading.Lock)
    next_command_id: int = 1
    actions: list[dict] = field(default_factory=list)  # results of past commands
    live_state: dict | None = None  # rebuilt every tick, never changed afterwards
    # webui settings are read once at program start: changing host or port
    # needs a restart.
    webui_enabled: bool = False
    webui_host: str = ""
    webui_port_setting: int = 0
    webui: ModuleType | None = None  # the started web UI, None when disabled
    webui_port: int | None = None  # the port really used


@dataclass
class EngineHandle:
    """All the web UI gets from the engine (REQ-34): three functions, no Engine.

    read_state() returns a copy of the latest result, live settings and run
    state.
    send_command(command) checks one command, queues it for the next tick and
    returns its id, or raises CommandError. The result shows up in
    read_state()["actions"] under that id.
    read_config() returns the settings form built from config.yaml (only
    reads the file).
    """

    read_state: Callable[[], dict]
    send_command: Callable[[dict], int]
    read_config: Callable[[], dict]


# --- 3. Starting the program (once) -------------------------------------------


def start_engine(config_path: Path, speed: float = 1.0) -> Engine:
    """Read the web UI settings and autostart, Play if autostart is true, then
    start the web UI if enabled.

    If Play fails at boot and the web UI is on, the error is kept and the
    emulator stays STOPPED, so it can be fixed from the Config tab. With the
    web UI off it is raised, so the program exits with the error.
    """
    config_path = Path(config_path)
    raw = read_config_file(config_path)
    webui_enabled, webui_host, webui_port = check_webui(raw)
    autostart = check_autostart(raw)
    engine = Engine(
        config_path=config_path,
        speed=speed,
        webui_enabled=webui_enabled,
        webui_host=webui_host,
        webui_port_setting=webui_port,
    )

    if autostart:
        try:
            play(engine)
        except (ValueError, OSError) as error:
            if not webui_enabled:
                raise
            engine.error = str(error)
            print(f"Config error: {error}", file=sys.stderr)
            print("Stays STOPPED. Fix it in the web UI Config tab, then press Play.")
    else:
        print("STOPPED (start.autostart is false). Press Play in the web UI.")

    engine.live_state = make_live_state(engine, None)  # before the first tick
    try:
        start_webui(engine)
    except BaseException:
        stop_interfaces(engine)
        raise
    return engine


def stop_engine(engine: Engine) -> None:
    """Stop the web UI first (no more commands), then every interface."""
    stop_webui(engine)
    stop_interfaces(engine)


# --- 4. Play and Stop (REQ-36) ------------------------------------------------


def prepare_run(raw: dict, config_folder: Path, speed: float):
    """Every check Play makes before any port is opened: the settings, the
    speed, each interface's contract and the battery files (by building the
    pack). Save calls exactly this too (REQ-16).

    Returns (config, interface modules, pack).
    """
    config = check_config(raw, config_folder)
    check_speed(config.interfaces, speed)
    modules = [load_interface(name) for name, _ in config.interfaces]
    pack = build_pack(config.battery_folder, config.pack_settings)
    return config, modules, pack


def play(engine: Engine) -> None:
    """Build a new pack and start the interfaces from the saved config.yaml.

    Live settings and the tick count start over. On any error the emulator
    stays STOPPED and the error is raised (interfaces already started are
    stopped again in start_interfaces).
    """
    if engine.run_state == RUNNING:
        raise CommandError("play: already RUNNING")
    raw = read_config_file(engine.config_path)
    config, modules, pack = prepare_run(raw, engine.config_path.parent, engine.speed)

    engine.config = config
    engine.pack = pack
    engine.tick_count = 0
    engine.last_result = None
    engine.current_source = config.current_source
    engine.simulated_a = config.simulated_a
    engine.command_a = 0.0
    engine.command_tick = None
    start_interfaces(engine, modules)

    engine.run_state = RUNNING
    engine.error = None
    print("RUNNING")


def stop(engine: Engine) -> None:
    """Close every interface, so no frames are sent. The pack and its last
    result stay, so the page can still show them."""
    if engine.run_state == STOPPED:
        raise CommandError("stop: already STOPPED")
    stop_interfaces(engine)
    engine.run_state = STOPPED
    print("STOPPED. Press Play in the web UI to start again.")


# --- 5. Saving settings (REQ-16, REQ-17) --------------------------------------


def save_settings(engine: Engine, value: dict) -> str:
    """Apply the page's changes to config.yaml, check them with the same
    checks Play uses, then write the file. Only while STOPPED. Returns the
    message for the page."""
    if engine.run_state == RUNNING:
        raise CommandError("save_config: press Stop before saving settings")
    document = apply_changes(engine.config_path, value["version"], value["changes"])
    prepare_run(document, engine.config_path.parent, engine.speed)
    write_config_file(engine.config_path, document)

    message = "Saved to config.yaml. Press Play to use the new settings."
    if any(path in RESTART_NEEDED for path in value["changes"]):
        message += (
            " webui.host and webui.port take effect after restarting the program."
        )
    return message


# --- 6. Interfaces ------------------------------------------------------------
# The engine finds an interface only by its name under interfaces: in
# config.yaml and calls only the contract functions (PROJECT.md). It never
# knows what CAN or UART is (REQ-14).


def load_interface(name: str) -> ModuleType:
    """Import interfaces.<name> and check it has every contract function."""
    module_name = f"interfaces.{name}"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as error:
        if error.name != module_name:
            raise  # the interface itself imports something missing
        raise ConfigError(f"interfaces.{name}: no folder interfaces/{name}") from None
    for function in CONTRACT:
        if not callable(getattr(module, function, None)):
            raise ConfigError(
                f"interfaces.{name}: interfaces/{name}/__init__.py has no "
                f"{function}() (see the interface contract in PROJECT.md)"
            )
    return module


def start_interfaces(engine: Engine, modules: list[ModuleType]) -> None:
    """Call start(settings, pack_info) on each. If one fails, stop the others.

    start() returns True if the interface can provide current commands. Those
    are remembered in current_sources, so the web UI can switch to them. The
    interface named by current.source must, otherwise the start stops here.
    It is remembered: step() reads the current from it.
    """
    info = pack_info(engine.pack)
    for module, (name, settings) in zip(modules, engine.config.interfaces, strict=True):
        try:
            provides_current = module.start(settings, info)
        except Exception:
            stop_interfaces(engine)
            raise
        engine.interfaces.append(module)
        if provides_current is True:
            engine.current_sources[name] = module

        if name != engine.config.current_source:
            continue
        if provides_current is not True:
            stop_interfaces(engine)  # including this one: it was started
            raise ConfigError(
                f"current.source '{name}' cannot provide current: interfaces.{name} "
                f"has no current input (its start() returned False)"
            )
        engine.current_interface = module


def send_to_interfaces(engine: Engine, result: PackResult) -> None:
    for module in engine.interfaces:
        module.send(result)


def stop_interfaces(engine: Engine) -> None:
    """Close every started interface, last started first."""
    while engine.interfaces:
        engine.interfaces.pop().stop()
    engine.current_interface = None
    engine.current_sources = {}


# --- 7. Web UI ----------------------------------------------------------------
# Loaded by name with importlib, only when webui.enabled is true, so the
# emulator never needs webui/ to run (REQ-15). It gets an EngineHandle with
# three functions and never the Engine, so the web thread cannot change the
# pack (REQ-34).


def start_webui(engine: Engine) -> None:
    """Import webui, check its contract and start it with a handle."""
    if not engine.webui_enabled:
        return
    module = importlib.import_module("webui")
    for function in WEBUI_CONTRACT:
        if not callable(getattr(module, function, None)):
            raise ConfigError(f"webui: webui/__init__.py has no {function}()")
    engine.webui_port = module.start(
        engine.webui_host, engine.webui_port_setting, make_handle(engine)
    )
    engine.webui = module
    print(f"Web UI on http://{engine.webui_host}:{engine.webui_port}")


def make_handle(engine: Engine) -> EngineHandle:
    """The three functions the web UI may call. All are safe from another
    thread: one copies the latest published state, one only puts into a
    queue, one only reads config.yaml."""

    def read_state() -> dict:
        return copy.deepcopy(engine.live_state)

    def send_command(command: dict) -> int:
        return queue_command(engine, command)

    def read_config() -> dict:
        return read_form(engine.config_path)

    return EngineHandle(
        read_state=read_state, send_command=send_command, read_config=read_config
    )


def stop_webui(engine: Engine) -> None:
    if engine.webui is not None:
        engine.webui.stop()
        engine.webui = None


# --- 8. Commands from the web UI ----------------------------------------------
# Checked on the web thread as they arrive, so a bad one gets a clear answer
# at once. The web thread only looks at the published live_state, never at
# the engine's own fields. Applied on the engine thread at the start of the
# next tick (REQ-34), and the result is kept in engine.actions for the page.


def check_command(engine: Engine, command) -> dict:
    """A clean copy of one command like {"command": "set_soc", "value": 50},
    or CommandError with a message saying what is wrong."""
    if not isinstance(command, dict):
        raise CommandError(
            'a command must be an object like {"command": ..., "value": ...}'
        )
    name = command.get("command")
    value = command.get("value")
    state = engine.live_state
    running = state["run_state"] == RUNNING

    if name not in COMMANDS:
        raise CommandError(f"unknown command '{name}' (known: {', '.join(COMMANDS)})")
    if name in LIVE_COMMANDS and not running:
        raise CommandError(f"{name}: only while RUNNING, press Play first")

    if name == "play" and running:
        raise CommandError("play: already RUNNING")
    elif name == "stop" and not running:
        raise CommandError("stop: already STOPPED")
    elif name == "save_config":
        if running:
            raise CommandError("save_config: press Stop before saving settings")
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("version"), str)
            or not isinstance(value.get("changes"), dict)
        ):
            raise CommandError(
                'save_config: value must be {"version": ..., "changes": {...}}'
            )
    elif name == "set_source":
        available = state["sources"]
        if value not in available:
            raise CommandError(
                f"source '{value}' must be 'simulated' or an interface that can "
                f"provide current (available: {', '.join(available)})"
            )
    elif name == "set_simulated_current":
        value = check_command_number(name, value)
    elif name == "set_soc":
        value = check_command_number(name, value)
        if not 0 <= value <= 100:
            raise CommandError(f"set_soc: SOC must be between 0 and 100, got {value}")
    elif name == "hold_soc" and not isinstance(value, bool):
        raise CommandError("hold_soc: value must be true or false")
    return {"command": name, "value": value}


def check_command_number(name: str, value) -> float:
    # bool counts as a number in Python; JSON also allows NaN and Infinity.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError(f"{name}: value must be a number")
    if not math.isfinite(value):
        raise CommandError(f"{name}: value must be a finite number")
    return float(value)


def queue_command(engine: Engine, command) -> int:
    """Web thread: check the command, give it an id, leave it for the engine
    thread. Several browsers can send at once, hence the lock around the id."""
    checked = check_command(engine, command)
    with engine.id_lock:
        checked["id"] = engine.next_command_id
        engine.next_command_id += 1
    engine.commands.put(checked)
    return checked["id"]


def apply_commands(engine: Engine) -> None:
    """Engine thread: apply every queued command, oldest first, and keep each
    result for the page. A command that fails never stops the emulator."""
    while True:
        try:
            command = engine.commands.get_nowait()
        except queue.Empty:
            return
        try:
            message = apply_command(engine, command)
            ok = True
        except (ValueError, OSError) as error:
            message = str(error)
            ok = False
            if command["command"] == "play":
                engine.error = message
        action = {
            "id": command["id"],
            "command": command["command"],
            "ok": ok,
            "message": message,
        }
        engine.actions.append(action)
        engine.actions = engine.actions[-ACTIONS_KEPT:]


def apply_command(engine: Engine, command: dict) -> str:
    """Do one command. Returns a short message, or raises with the reason.

    The run state is checked again here: it may have changed since the web
    thread checked the command (for example Play queued twice).
    """
    name, value = command["command"], command["value"]
    if name == "play":
        play(engine)
        return "RUNNING"
    if name == "stop":
        stop(engine)
        return "STOPPED"
    if name == "save_config":
        return save_settings(engine, value)

    if engine.run_state != RUNNING:
        raise CommandError(f"{name}: only while RUNNING, press Play first")
    if name == "set_source":
        if value != "simulated" and value not in engine.current_sources:
            raise CommandError(f"source '{value}' is not available any more")
        switch_source(engine, value)
    elif name == "set_simulated_current":
        engine.simulated_a = value
    elif name == "set_soc":
        set_soc(engine.pack, value)
    elif name == "hold_soc":
        set_hold_soc(engine.pack, value)
    return "applied"


def switch_source(engine: Engine, source: str) -> None:
    """Change where the current comes from.

    An interface starts in timeout, like at start (REQ-10): a command that
    waited while it was not the source is read once and thrown away, so only
    a fresh one counts.
    """
    engine.current_source = source
    engine.current_interface = engine.current_sources.get(source)
    engine.command_a = 0.0
    engine.command_tick = None
    if engine.current_interface is not None:
        engine.current_interface.read_current()  # drop anything old


# --- 9. One tick --------------------------------------------------------------


def step(engine: Engine) -> PackResult | None:
    """One pass of the loop. No sleeping; prints only when Play or Stop
    changes the run state.

    First apply the commands the web UI queued since the last pass (REQ-34).
    While STOPPED that is all: no tick, None is returned. While RUNNING, pick
    the current (simulated or from an interface, with the timeout) and move
    the pack by TICK_S seconds. The deadband is applied afterwards in pack.py.
    Last, publish a fresh live state for the web UI.
    """
    apply_commands(engine)
    if engine.run_state != RUNNING:
        engine.live_state = make_live_state(engine, engine.last_result)
        return None

    current_a, timed_out = pick_current(engine)
    result = tick(engine.pack, current_a, TICK_S)
    result.current_timeout = timed_out  # pack.py knows nothing about sources
    engine.tick_count += 1
    engine.last_result = result
    engine.live_state = make_live_state(engine, result)
    return result


def make_live_state(engine: Engine, result: PackResult | None) -> dict:
    """The latest result, live settings and run state as plain values (ready
    for JSON).

    A new dict every pass, never changed after it is published: the web
    thread reads it while the engine thread is already building the next one.
    """
    return {
        "run_state": engine.run_state,
        "error": engine.error,
        "actions": list(engine.actions),
        "result": None if result is None else asdict(result),
        "elapsed_s": elapsed_s(engine),
        "current_source": engine.current_source,
        "simulated_a": engine.simulated_a,
        "hold_soc": engine.pack is not None and engine.pack.hold_soc,
        "sources": ["simulated", *engine.current_sources],
    }


def pick_current(engine: Engine) -> tuple[float, bool]:
    """The current for this tick and whether the timeout flag is set.

    Simulated: the live simulated current (from config.yaml, or changed in the
    web UI), like a command every tick (REQ-09).
    Interface: a new value from read_current() becomes the current and resets
    the timeout. With nothing new, the last command is held until more than
    timeout_ms has passed; then the current is 0 and the flag is set (REQ-10).
    Before the first command the emulator is in timeout.

    Time is counted in ticks, so it is emulated time. With an interface
    enabled, --speed is always 1 (REQ-26), so that equals real time.
    """
    if engine.current_interface is None:
        return engine.simulated_a, False

    new_value = engine.current_interface.read_current()
    if new_value is not None:
        engine.command_a = float(new_value)
        engine.command_tick = engine.tick_count

    if engine.command_tick is None:
        return 0.0, True  # no command has arrived yet
    since_command_ms = (engine.tick_count - engine.command_tick) * TICK_S * 1000
    if since_command_ms > engine.config.timeout_ms:
        return 0.0, True
    return engine.command_a, False


def elapsed_s(engine: Engine) -> float:
    """Emulated time since the last Play. Depends only on the number of ticks."""
    return engine.tick_count * TICK_S


# --- 10. Status line ----------------------------------------------------------


def status_line(engine: Engine, result: PackResult) -> str:
    """One line with the main pack values. M = module, C = cell, both from 1.

    TIMEOUT goes at the end, so the columns before it never move (REQ-25).
    """
    low = result.lowest_cell
    high = result.highest_cell
    line = (
        f"t={elapsed_s(engine):8.1f} s  {result.state:<11}  "
        f"I={result.current_a:7.1f} A  V={result.voltage_v:8.2f} V  "
        f"SOC={result.soc_pct:6.2f} %  "
        f"min {low.voltage_v:.3f} V (M{low.module} C{low.cell})  "
        f"max {high.voltage_v:.3f} V (M{high.module} C{high.cell})"
    )
    if result.current_timeout:
        line += "  TIMEOUT"
    return line


def summary_line(engine: Engine) -> str:
    """Printed once when the loop stops."""
    text = (
        f"Stopped after {elapsed_s(engine):.1f} s emulated ({engine.tick_count} steps)."
    )
    if engine.last_result is not None:
        result = engine.last_result
        text += f" State {result.state}, SOC {result.soc_pct:.2f} %."
    return text


# --- 11. The loop -------------------------------------------------------------


def run(
    engine: Engine,
    speed: float = 1.0,
    max_ticks: int | None = None,
    clock=time.monotonic,
    sleep=time.sleep,
    output=print,
    status_every_s: int = 1,
) -> None:
    """Call step() every TICK_S seconds of real time, divided by speed.

    Each deadline is counted from the start time, not from "now", so small
    delays never add up (no drift). If a tick is late, the next one runs at
    once to catch up. Speed only changes how long the loop sleeps; every step
    still moves the pack by TICK_S of emulated time. While STOPPED the loop
    keeps its pace (so queued commands like Play are picked up) but no tick
    runs; max_ticks counts loop passes.

    A status line is printed every status_every_s seconds of emulated time.
    max_ticks, clock, sleep and output let tests run the loop without waiting.
    """
    real_tick_s = TICK_S / speed
    ticks_per_status = status_every_s * TICKS_PER_SECOND
    start_time = clock()
    ticks_done = 0

    while max_ticks is None or ticks_done < max_ticks:
        result = step(engine)
        ticks_done += 1

        # STOPPED: no tick happened, so nothing is sent or printed (REQ-36).
        if result is not None:
            send_to_interfaces(engine, result)
            if engine.tick_count % ticks_per_status == 0:
                output(status_line(engine, result))

        next_deadline = start_time + ticks_done * real_tick_s
        sleep(max(0.0, next_deadline - clock()))


# --- 12. Stopping on SIGTERM --------------------------------------------------
# Ctrl+C raises KeyboardInterrupt. systemd stops a service with SIGTERM
# instead, which by default ends Python at once, without calling stop() on the
# interfaces. This handler turns SIGTERM into an exception, so both ways of
# stopping take the same clean path in main() (REQ-33).


class StopRequested(Exception):
    """Raised by the SIGTERM handler to leave the loop."""


def on_sigterm(signal_number, frame) -> None:
    raise StopRequested


# --- 13. Command line ---------------------------------------------------------


def positive_speed(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("--speed must be a number") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("--speed must be greater than 0")
    return value


def positive_whole_number(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "--status-every must be a whole number"
        ) from None
    if value < 1:
        raise argparse.ArgumentTypeError("--status-every must be at least 1")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m emulator.engine",
        description="Run the battery emulator in the terminal.",
    )
    parser.add_argument(
        "--config", default="config.yaml", help="settings file (default config.yaml)"
    )
    parser.add_argument(
        "--speed",
        type=positive_speed,
        default=1.0,
        help="run faster than real time, e.g. 10 (default 1)",
    )
    parser.add_argument(
        "--status-every",
        type=positive_whole_number,
        default=1,
        help="print the status line every N seconds of emulated time (default 1)",
    )
    args = parser.parse_args(argv)

    # Config, battery file and interface start errors are shown as one line,
    # no traceback.
    try:
        engine = start_engine(Path(args.config), speed=args.speed)
    except ValueError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 1

    previous_handler = signal.signal(signal.SIGTERM, on_sigterm)
    try:
        run(engine, speed=args.speed, status_every_s=args.status_every)
    except (KeyboardInterrupt, StopRequested):
        print()  # end the line after the "^C" the terminal shows on Ctrl+C
        print(summary_line(engine))
    except ValueError as error:  # an interface that refuses a value (CAN never does)
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        stop_engine(engine)  # web UI and every interface
        signal.signal(signal.SIGTERM, previous_handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
