"""High level: config, current source and time.

Reads config.yaml, picks the current (from an interface or simulated), runs the
100 ms loop and hands each result to the enabled interfaces. Uses only pack.py.

Run it from the project folder:
    uv run python -m emulator.engine --config config.yaml --speed 1

Units: SOC in percent (0 to 100), current in amps with discharge positive,
temperature in degrees C, voltage in volts, time in seconds.
"""

import argparse
import importlib
import sys
import time
from dataclasses import dataclass, field
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
    tick,
)

# --- 1. Constants -------------------------------------------------------------

TICK_S = 0.1  # emulated time per step; the speed factor never changes this
TICKS_PER_STATUS = 10  # one status line per second of emulated time
CONTRACT = ("start", "send", "read_current", "stop")  # interface functions


# --- 2. Data shapes -----------------------------------------------------------


class ConfigError(ValueError):
    """A bad value in config.yaml. The message starts with the setting name."""


@dataclass
class EngineConfig:
    """The checked values from config.yaml that the engine uses."""

    battery_folder: Path
    pack_settings: PackSettings
    current_source: str  # "simulated" or the name of an enabled interface
    simulated_a: float
    timeout_ms: float  # interface current timeout (REQ-10)
    interfaces: list[tuple[str, dict]]  # (name, settings) of each enabled one


@dataclass
class Engine:
    config: EngineConfig
    pack: Pack
    interfaces: list[ModuleType] = field(default_factory=list)  # started ones
    tick_count: int = 0
    last_result: PackResult | None = None
    # Current from an interface (REQ-08, REQ-10). Unused when simulated.
    current_interface: ModuleType | None = None  # the one named by current.source
    command_a: float = 0.0  # the last current command received
    command_tick: int | None = None  # tick_count when it arrived; None = none yet


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

    return EngineConfig(
        battery_folder=battery_folder,
        pack_settings=pack_settings,
        current_source=check_current_source(raw, enabled_names),
        simulated_a=get_number(raw, "current", "simulated_a"),
        timeout_ms=get_number(raw, "current", "timeout_ms", above=0),
        interfaces=interfaces,
    )


# --- 5. Building the engine (once, at start) ----------------------------------


def start_engine(config_path: Path, speed: float = 1.0) -> Engine:
    """Read and check config.yaml, build the pack, then start the interfaces.

    Every check runs before any port is opened.
    """
    config_path = Path(config_path)
    raw = read_config_file(config_path)
    config = check_config(raw, config_path.parent)
    check_speed(config.interfaces, speed)
    modules = [load_interface(name) for name, _ in config.interfaces]
    pack = build_pack(config.battery_folder, config.pack_settings)
    engine = Engine(config=config, pack=pack)
    start_interfaces(engine, modules)
    return engine


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

    start() returns True if the interface can provide current commands. The
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


# --- 7. One tick --------------------------------------------------------------


def step(engine: Engine) -> PackResult:
    """Run exactly one tick of TICK_S seconds. No sleeping, no printing.

    First pick the current (simulated or from an interface, with the timeout),
    then move the pack. The deadband is applied afterwards in pack.py.
    """
    current_a, timed_out = pick_current(engine)
    result = tick(engine.pack, current_a, TICK_S)
    result.current_timeout = timed_out  # pack.py knows nothing about sources
    engine.tick_count += 1
    engine.last_result = result
    return result


def pick_current(engine: Engine) -> tuple[float, bool]:
    """The current for this tick and whether the timeout flag is set.

    Simulated: the value from config.yaml, like a command every tick (REQ-09).
    Interface: a new value from read_current() becomes the current and resets
    the timeout. With nothing new, the last command is held until more than
    timeout_ms has passed; then the current is 0 and the flag is set (REQ-10).
    Before the first command the emulator is in timeout.

    Time is counted in ticks, so it is emulated time. With an interface
    enabled, --speed is always 1 (REQ-26), so that equals real time.
    """
    if engine.current_interface is None:
        return engine.config.simulated_a, False

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


# --- 8. Status line -----------------------------------------------------------


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


# --- 9. The loop --------------------------------------------------------------


def run(
    engine: Engine,
    speed: float = 1.0,
    max_ticks: int | None = None,
    clock=time.monotonic,
    sleep=time.sleep,
    output=print,
) -> None:
    """Call step() every TICK_S seconds of real time, divided by speed.

    Each deadline is counted from the start time, not from "now", so small
    delays never add up (no drift). If a tick is late, the next one runs at
    once to catch up. Speed only changes how long the loop sleeps; every step
    still moves the pack by TICK_S of emulated time.

    max_ticks, clock, sleep and output let tests run the loop without waiting.
    """
    real_tick_s = TICK_S / speed
    start_time = clock()
    ticks_done = 0

    while max_ticks is None or ticks_done < max_ticks:
        result = step(engine)
        ticks_done += 1

        send_to_interfaces(engine, result)

        if engine.tick_count % TICKS_PER_STATUS == 0:
            output(status_line(engine, result))

        next_deadline = start_time + ticks_done * real_tick_s
        sleep(max(0.0, next_deadline - clock()))


# --- 10. Command line ---------------------------------------------------------


def positive_speed(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError("--speed must be a number") from None
    if value <= 0:
        raise argparse.ArgumentTypeError("--speed must be greater than 0")
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
    args = parser.parse_args(argv)

    # Config, battery file and interface start errors are shown as one line,
    # no traceback.
    try:
        engine = start_engine(Path(args.config), speed=args.speed)
    except ValueError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 1

    try:
        run(engine, speed=args.speed)
    except KeyboardInterrupt:
        print()  # end the line after the "^C" the terminal shows
        print(summary_line(engine))
    except ValueError as error:  # an interface that refuses a value (CAN never does)
        print(f"Error: {error}", file=sys.stderr)
        return 1
    finally:
        stop_interfaces(engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
