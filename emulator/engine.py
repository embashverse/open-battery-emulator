"""High level: config, current source and time.

Reads config.yaml, picks the current (from an interface or simulated), runs the
100 ms loop and hands each result to the enabled interfaces. Uses only pack.py.

Run it from the project folder:
    uv run python -m emulator.engine --config config.yaml --speed 1

Units: SOC in percent (0 to 100), current in amps with discharge positive,
temperature in degrees C, voltage in volts, time in seconds.
"""

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from emulator.pack import Pack, PackResult, PackSettings, build_pack, tick

# --- 1. Constants -------------------------------------------------------------

TICK_S = 0.1  # emulated time per step; the speed factor never changes this
TICKS_PER_STATUS = 10  # one status line per second of emulated time


# --- 2. Data shapes -----------------------------------------------------------


class ConfigError(ValueError):
    """A bad value in config.yaml. The message starts with the setting name."""


@dataclass
class EngineConfig:
    """The checked values from config.yaml that the engine uses."""

    battery_folder: Path
    pack_settings: PackSettings
    current_source: str
    simulated_a: float
    timeout_ms: float  # not used until M3 (interface timeout, REQ-10)


@dataclass
class Engine:
    config: EngineConfig
    pack: Pack
    tick_count: int = 0
    last_result: PackResult | None = None


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


def check_current_source(raw: dict) -> str:
    """Only simulated current exists in M2. The interfaces section is ignored."""
    source = get_value(raw, "current", "source")
    if source != "simulated":
        raise ConfigError(
            f"current.source '{source}' is not available yet (comes with M3)"
        )
    return source


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

    return EngineConfig(
        battery_folder=battery_folder,
        pack_settings=pack_settings,
        current_source=check_current_source(raw),
        simulated_a=get_number(raw, "current", "simulated_a"),
        timeout_ms=get_number(raw, "current", "timeout_ms", above=0),
    )


# --- 5. Building the engine (once, at start) ----------------------------------


def start_engine(config_path: Path) -> Engine:
    """Read and check config.yaml, then build the pack through pack.py."""
    config_path = Path(config_path)
    raw = read_config_file(config_path)
    config = check_config(raw, config_path.parent)
    pack = build_pack(config.battery_folder, config.pack_settings)
    return Engine(config=config, pack=pack)


# --- 6. One tick --------------------------------------------------------------


def step(engine: Engine) -> PackResult:
    """Run exactly one tick of TICK_S seconds. No sleeping, no printing.

    In M2 the current is always the simulated one from config.yaml, so it acts
    like a current command arriving every tick (REQ-09).
    """
    current_a = engine.config.simulated_a
    result = tick(engine.pack, current_a, TICK_S)
    engine.tick_count += 1
    engine.last_result = result
    return result


def elapsed_s(engine: Engine) -> float:
    """Emulated time so far. Depends only on the number of ticks."""
    return engine.tick_count * TICK_S


# --- 7. Status line -----------------------------------------------------------


def status_line(engine: Engine, result: PackResult) -> str:
    """One line with the main pack values. M = module, C = cell, both from 1."""
    low = result.lowest_cell
    high = result.highest_cell
    return (
        f"t={elapsed_s(engine):8.1f} s  {result.state:<11}  "
        f"I={result.current_a:7.1f} A  V={result.voltage_v:8.2f} V  "
        f"SOC={result.soc_pct:6.2f} %  "
        f"min {low.voltage_v:.3f} V (M{low.module} C{low.cell})  "
        f"max {high.voltage_v:.3f} V (M{high.module} C{high.cell})"
    )


def summary_line(engine: Engine) -> str:
    """Printed once when the loop stops."""
    text = (
        f"Stopped after {elapsed_s(engine):.1f} s emulated ({engine.tick_count} steps)."
    )
    if engine.last_result is not None:
        result = engine.last_result
        text += f" State {result.state}, SOC {result.soc_pct:.2f} %."
    return text


# --- 8. The loop --------------------------------------------------------------


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

        # --- M3: interfaces plug in here ---------------------------------
        # Following the interface contract in PROJECT.md:
        #   - before this loop, call start(settings) once for every enabled
        #     interface, with its section of config.yaml;
        #   - here, call send(result) on every enabled interface;
        #   - here, if current.source names an interface, call its
        #     read_current() and use that value for the next step(); if no
        #     command arrived within current.timeout_ms, use 0 A and set the
        #     timeout flag (REQ-10).
        # The engine calls only these three functions, never CAN or UART code.
        # ------------------------------------------------------------------

        if engine.tick_count % TICKS_PER_STATUS == 0:
            output(status_line(engine, result))

        next_deadline = start_time + ticks_done * real_tick_s
        sleep(max(0.0, next_deadline - clock()))


# --- 9. Command line ----------------------------------------------------------


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

    # Config and battery file errors are shown as one line, no traceback.
    try:
        engine = start_engine(Path(args.config))
    except ValueError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 1

    try:
        run(engine, speed=args.speed)
    except KeyboardInterrupt:
        print()  # end the line after the "^C" the terminal shows
        print(summary_line(engine))
    return 0


if __name__ == "__main__":
    sys.exit(main())
