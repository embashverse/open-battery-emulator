"""High level: config, current source and time.

Reads config.yaml, picks the current (from an interface or simulated), runs the
100 ms loop and hands each result to the enabled interfaces. Uses only pack.py.

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
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import ModuleType

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from emulator.pack import (
    Pack,
    PackResult,
    PackSettings,
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
WEBUI_DEFAULT_HOST = "0.0.0.0"  # reachable from other devices on the network
WEBUI_DEFAULT_PORT = 8080
# Commands the web UI can queue (REQ-21, REQ-34), checked in check_command().
COMMANDS = ("set_source", "set_simulated_current", "set_soc", "hold_soc")


# --- 2. Data shapes -----------------------------------------------------------


class ConfigError(ValueError):
    """A bad value in config.yaml. The message starts with the setting name."""


class CommandError(ValueError):
    """A command from the web UI that cannot be applied. The message says why."""


@dataclass
class EngineConfig:
    """The checked values from config.yaml that the engine uses."""

    battery_folder: Path
    pack_settings: PackSettings
    current_source: str  # "simulated" or the name of an enabled interface
    simulated_a: float
    timeout_ms: float  # interface current timeout (REQ-10)
    interfaces: list[tuple[str, dict]]  # (name, settings) of each enabled one
    webui_enabled: bool = False
    webui_host: str = WEBUI_DEFAULT_HOST
    webui_port: int = WEBUI_DEFAULT_PORT


@dataclass
class Engine:
    config: EngineConfig
    pack: Pack
    interfaces: list[ModuleType] = field(default_factory=list)  # started ones
    tick_count: int = 0
    last_result: PackResult | None = None
    # Live settings: they start from config.yaml and the web UI can change them
    # while running. They are never written back to config.yaml.
    current_source: str = "simulated"
    simulated_a: float = 0.0
    # Current from an interface (REQ-08, REQ-10). Unused when simulated.
    current_interface: ModuleType | None = None  # the one named by current_source
    current_sources: dict[str, ModuleType] = field(default_factory=dict)  # can provide
    command_a: float = 0.0  # the last current command received
    command_tick: int | None = None  # tick_count when it arrived; None = none yet
    # Web UI (REQ-15, REQ-34). The web thread only puts into `commands` and
    # reads `live_state`; everything else is used by the engine thread only.
    commands: queue.Queue = field(default_factory=queue.Queue)
    live_state: dict | None = None  # rebuilt every tick, never changed afterwards
    webui: ModuleType | None = None  # the started web UI, None when disabled
    webui_port: int | None = None  # the port really used


@dataclass
class EngineHandle:
    """All the web UI gets from the engine (REQ-34): two functions, no Engine.

    read_state() returns a copy of the latest result and live settings.
    send_command(command) checks one command and queues it for the next tick,
    or raises CommandError.
    """

    read_state: Callable[[], dict]
    send_command: Callable[[dict], None]


# --- 3. Reading config.yaml ---------------------------------------------------


def read_config_file(path: Path) -> dict:
    """Load config.yaml as nested dictionaries.

    Round trip mode ("rt") is the mode that keeps comments, which the web UI
    needs in M5 when it saves the file. Reading uses the same mode.
    """
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        raw = YAML(typ="rt").load(path.read_text())
    except YAMLError as error:
        raise ConfigError(f"{path} is not valid YAML: {error}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain settings as 'name: value' lines")
    return raw


# --- 4. Checking config values ------------------------------------------------
# Every error names the setting, for example "pack.cells_per_module".


def get_value(raw: dict, section: str, key: str):
    """The value of section.key, or a clear error if it is missing."""
    values = raw.get(section)
    if not isinstance(values, dict):
        raise ConfigError(f"{section} is missing")
    if key not in values or values[key] is None:
        raise ConfigError(f"{section}.{key} is missing")
    return values[key]


def get_whole_number(raw: dict, section: str, key: str, minimum: int | None = None):
    value = get_value(raw, section, key)
    # bool counts as an int in Python, so "true" would pass as 1 without this.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{section}.{key} must be a whole number")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{section}.{key} must be at least {minimum}")
    return int(value)


def get_number(
    raw: dict,
    section: str,
    key: str,
    minimum: float | None = None,
    maximum: float | None = None,
    above: float | None = None,
) -> float:
    """A number, optionally at least `minimum`, at most `maximum`, or above `above`."""
    value = get_value(raw, section, key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{section}.{key} must be a number")
    name = f"{section}.{key}"
    if minimum is not None and maximum is not None:
        if not minimum <= value <= maximum:
            raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    elif minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    if above is not None and value <= above:
        raise ConfigError(f"{name} must be greater than {above}")
    return float(value)


def check_battery(raw: dict, config_folder: Path) -> Path:
    """The battery folder, found in batteries/ next to config.yaml."""
    name = raw.get("battery")
    if name is None:
        raise ConfigError("battery is missing")
    if not isinstance(name, str):
        raise ConfigError("battery must be the name of a folder in batteries/")
    folder = config_folder / "batteries" / name
    if not folder.is_dir():
        raise ConfigError(f"battery: no folder batteries/{name}")
    return folder


def check_current_source(raw: dict, enabled_names: list[str]) -> str:
    """REQ-08: "simulated", or the name of an interface with enabled: true."""
    source = get_value(raw, "current", "source")
    if source == "simulated" or source in enabled_names:
        return source
    enabled = ", ".join(enabled_names) or "none"
    raise ConfigError(
        f"current.source '{source}' must be 'simulated' or the name of an "
        f"enabled interface (enabled: {enabled})"
    )


def check_interfaces(raw: dict, config_folder: Path) -> list[tuple[str, dict]]:
    """Name and settings of every interface with enabled: true.

    The engine does not know what any setting means. It only makes relative
    file paths full (see resolve_paths), so interfaces get usable paths.
    """
    sections = raw.get("interfaces")
    if sections is None:
        return []  # no interfaces section, or every interface commented out
    if not isinstance(sections, dict):
        raise ConfigError("interfaces must contain one section per interface")

    enabled = []
    for name, settings in sections.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ConfigError(f"interfaces.{name}: not a valid interface name")
        if not isinstance(settings, dict):
            raise ConfigError(f"interfaces.{name} must contain its settings")
        switch = settings.get("enabled")
        if switch is None:
            raise ConfigError(f"interfaces.{name}.enabled is missing")
        if not isinstance(switch, bool):
            raise ConfigError(f"interfaces.{name}.enabled must be true or false")
        if switch:
            enabled.append((name, resolve_paths(settings, config_folder)))
    return enabled


def resolve_paths(settings: dict, config_folder: Path) -> dict:
    """A copy of settings where every relative string that names an existing
    file next to config.yaml is replaced by its full path. Nested sections are
    handled the same way. Everything else is copied unchanged."""
    resolved = {}
    for key, value in settings.items():
        if isinstance(value, dict):
            value = resolve_paths(value, config_folder)
        elif isinstance(value, str) and not Path(value).is_absolute():
            candidate = config_folder / value
            if candidate.is_file():
                value = str(candidate.resolve())
        resolved[key] = value
    return resolved


def check_webui(raw: dict) -> tuple[bool, str, int]:
    """REQ-15: enabled, host and port of the web UI.

    No webui section means it is off. host and port have defaults. Port 0
    means "any free port" (used by the tests).
    """
    section = raw.get("webui")
    if section is None:
        return False, WEBUI_DEFAULT_HOST, WEBUI_DEFAULT_PORT
    if not isinstance(section, dict):
        raise ConfigError("webui must contain its settings")

    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("webui.enabled must be true or false")

    host = section.get("host", WEBUI_DEFAULT_HOST)
    if not isinstance(host, str) or not host:
        raise ConfigError("webui.host must be an address like 0.0.0.0 or 127.0.0.1")

    port = section.get("port", WEBUI_DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ConfigError("webui.port must be a whole number from 0 to 65535")
    return enabled, host, int(port)


def check_config(raw: dict, config_folder: Path) -> EngineConfig:
    """Check every setting the engine uses and collect them in one place."""
    battery_folder = check_battery(raw, config_folder)

    pack_settings = PackSettings(
        cells_per_module=get_whole_number(raw, "pack", "cells_per_module", minimum=1),
        modules_in_series=get_whole_number(raw, "pack", "modules_in_series", minimum=1),
        strings_in_parallel=get_whole_number(
            raw, "pack", "strings_in_parallel", minimum=1
        ),
        start_soc_pct=get_number(raw, "start", "soc", minimum=0, maximum=100),
        temperature_c=get_number(raw, "start", "temperature_c"),
        capacity_variation_pct=get_number(raw, "variation", "capacity_pct", minimum=0),
        r0_variation_pct=get_number(raw, "variation", "r0_pct", minimum=0),
        soc_variation_pct=get_number(raw, "variation", "soc_pct", minimum=0),
        seed=get_whole_number(raw, "variation", "seed"),
        deadband_a=get_number(raw, "current", "deadband_a", minimum=0),
    )

    # Interfaces first: current.source may name one of them.
    interfaces = check_interfaces(raw, config_folder)
    enabled_names = [name for name, _ in interfaces]
    webui_enabled, webui_host, webui_port = check_webui(raw)

    return EngineConfig(
        battery_folder=battery_folder,
        pack_settings=pack_settings,
        current_source=check_current_source(raw, enabled_names),
        simulated_a=get_number(raw, "current", "simulated_a"),
        timeout_ms=get_number(raw, "current", "timeout_ms", above=0),
        interfaces=interfaces,
        webui_enabled=webui_enabled,
        webui_host=webui_host,
        webui_port=webui_port,
    )


# --- 5. Building the engine (once, at start) ----------------------------------


def start_engine(config_path: Path, speed: float = 1.0) -> Engine:
    """Read and check config.yaml, build the pack, start the interfaces, then
    the web UI if enabled (it needs to know which sources can provide current).

    Every check runs before any port is opened. If the web UI cannot start,
    the interfaces are stopped again.
    """
    config_path = Path(config_path)
    raw = read_config_file(config_path)
    config = check_config(raw, config_path.parent)
    check_speed(config.interfaces, speed)
    modules = [load_interface(name) for name, _ in config.interfaces]
    pack = build_pack(config.battery_folder, config.pack_settings)
    engine = Engine(
        config=config,
        pack=pack,
        current_source=config.current_source,
        simulated_a=config.simulated_a,
    )
    start_interfaces(engine, modules)
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


# --- 6. Interfaces ------------------------------------------------------------
# The engine finds an interface only by its name under interfaces: in
# config.yaml and calls only the contract functions (PROJECT.md). It never
# knows what CAN or UART is (REQ-14).


def check_speed(interfaces: list[tuple[str, dict]], speed: float) -> None:
    """REQ-26: with a device on the other end, time must be real time."""
    if interfaces and speed != 1:
        names = ", ".join(name for name, _ in interfaces)
        raise ConfigError(
            f"--speed must be 1 when an interface is enabled (enabled: {names})"
        )


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
# two functions and never the Engine, so the web thread cannot change the
# pack (REQ-34).


def start_webui(engine: Engine) -> None:
    """Import webui, check its contract and start it with a handle."""
    config = engine.config
    if not config.webui_enabled:
        return
    module = importlib.import_module("webui")
    for function in WEBUI_CONTRACT:
        if not callable(getattr(module, function, None)):
            raise ConfigError(f"webui: webui/__init__.py has no {function}()")
    engine.webui_port = module.start(
        config.webui_host, config.webui_port, make_handle(engine)
    )
    engine.webui = module
    print(f"Web UI on http://{config.webui_host}:{engine.webui_port}")


def make_handle(engine: Engine) -> EngineHandle:
    """The two functions the web UI may call. Both are safe from another thread:
    one copies the latest published state, the other only puts into a queue."""

    def read_state() -> dict:
        return copy.deepcopy(engine.live_state)

    def send_command(command: dict) -> None:
        queue_command(engine, command)

    return EngineHandle(read_state=read_state, send_command=send_command)


def stop_webui(engine: Engine) -> None:
    if engine.webui is not None:
        engine.webui.stop()
        engine.webui = None


# --- 8. Commands from the web UI ----------------------------------------------
# Checked on the web thread as they arrive, so a bad one gets a clear answer
# at once. Applied on the engine thread at the start of the next tick (REQ-34).


def check_command(engine: Engine, command) -> dict:
    """A clean copy of one command like {"command": "set_soc", "value": 50},
    or CommandError with a message saying what is wrong."""
    if not isinstance(command, dict):
        raise CommandError(
            'a command must be an object like {"command": ..., "value": ...}'
        )
    name = command.get("command")
    value = command.get("value")

    if name == "set_source":
        # current_sources is filled once at start and only read here.
        available = ["simulated", *engine.current_sources]
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
    elif name == "hold_soc":
        if not isinstance(value, bool):
            raise CommandError("hold_soc: value must be true or false")
    else:
        raise CommandError(f"unknown command '{name}' (known: {', '.join(COMMANDS)})")
    return {"command": name, "value": value}


def check_command_number(name: str, value) -> float:
    # bool counts as a number in Python; JSON also allows NaN and Infinity.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError(f"{name}: value must be a number")
    if not math.isfinite(value):
        raise CommandError(f"{name}: value must be a finite number")
    return float(value)


def queue_command(engine: Engine, command) -> None:
    """Web thread: check the command, then leave it for the engine thread."""
    engine.commands.put(check_command(engine, command))


def apply_commands(engine: Engine) -> None:
    """Engine thread: apply every queued command, oldest first."""
    while True:
        try:
            command = engine.commands.get_nowait()
        except queue.Empty:
            return
        name, value = command["command"], command["value"]
        if name == "set_source":
            switch_source(engine, value)
        elif name == "set_simulated_current":
            engine.simulated_a = value
        elif name == "set_soc":
            set_soc(engine.pack, value)
        elif name == "hold_soc":
            set_hold_soc(engine.pack, value)


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


def step(engine: Engine) -> PackResult:
    """Run exactly one tick of TICK_S seconds. No sleeping, no printing.

    First apply the commands the web UI queued since the last tick (REQ-34),
    then pick the current (simulated or from an interface, with the timeout),
    then move the pack. The deadband is applied afterwards in pack.py. Last,
    publish a fresh live state for the web UI.
    """
    apply_commands(engine)
    current_a, timed_out = pick_current(engine)
    result = tick(engine.pack, current_a, TICK_S)
    result.current_timeout = timed_out  # pack.py knows nothing about sources
    engine.tick_count += 1
    engine.last_result = result
    engine.live_state = make_live_state(engine, result)
    return result


def make_live_state(engine: Engine, result: PackResult | None) -> dict:
    """The latest result and live settings as plain values (ready for JSON).

    A new dict every tick, never changed after it is published: the web
    thread reads it while the engine thread is already building the next one.
    """
    return {
        "result": None if result is None else asdict(result),
        "elapsed_s": elapsed_s(engine),
        "current_source": engine.current_source,
        "simulated_a": engine.simulated_a,
        "hold_soc": engine.pack.hold_soc,
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
    """Emulated time so far. Depends only on the number of ticks."""
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
    still moves the pack by TICK_S of emulated time.

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
