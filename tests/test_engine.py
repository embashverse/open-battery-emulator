"""Tests for emulator/engine.py: config, one step, the loop, status line, command line.

Battery data and config.yaml are written in tmp_path, so these tests do not
depend on batteries/ or the real config.yaml. No test sleeps: the loop gets a
fake clock.
"""

import re

import pytest
from ruamel.yaml import YAML

from emulator.engine import (
    TICK_S,
    ConfigError,
    elapsed_s,
    main,
    run,
    start_engine,
    status_line,
    step,
)

# --- Small test battery -------------------------------------------------------
# OCV is a straight line: 3.0 V at SOC 0 to 3.5 V at SOC 100 (charge curve
# 0.1 V higher). v_min and v_max are set wide, so the pack never goes EMPTY or
# FULL in these tests: even at SOC 0 and 100 A a cell shows 2.9 V.

CELL_CSV = """parameter,value,unit
capacity,100,Ah
r0,0.001,ohm
v_min,2.5,V
v_max,3.65,V
"""

OCV_DISCHARGE = """soc,0,40
0,3.0,3.0
100,3.5,3.5
"""

OCV_CHARGE = """soc,0,40
0,3.1,3.1
100,3.6,3.6
"""

LIMIT = """soc,0,40
0,100,100
100,100,100
"""


def write_battery(config_folder, name="test"):
    folder = config_folder / "batteries" / name
    folder.mkdir(parents=True)
    (folder / "cell.csv").write_text(CELL_CSV)
    (folder / "ocv_charge.csv").write_text(OCV_CHARGE)
    (folder / "ocv_discharge.csv").write_text(OCV_DISCHARGE)
    (folder / "limit_charge.csv").write_text(LIMIT)
    (folder / "limit_discharge.csv").write_text(LIMIT)


# --- Small test config --------------------------------------------------------

MISSING = object()  # use as a value to leave a setting out


def base_config():
    """One cell, SOC 50, 25 degC, no variation, 10 A simulated."""
    return {
        "battery": "test",
        "pack": {
            "cells_per_module": 1,
            "modules_in_series": 1,
            "strings_in_parallel": 1,
        },
        "start": {"soc": 50, "temperature_c": 25},
        "variation": {"capacity_pct": 0, "r0_pct": 0, "soc_pct": 0, "seed": 1},
        "current": {
            "source": "simulated",
            "simulated_a": 10,
            "timeout_ms": 500,
            "deadband_a": 0.5,
        },
    }


def write_config(tmp_path, changes=None):
    """Write battery and config.yaml in tmp_path. changes: {"pack.cells_per_module": 0}."""
    write_battery(tmp_path)
    config = base_config()
    for name, value in (changes or {}).items():
        if "." in name:
            section, key = name.split(".")
            target = config[section]
        else:
            key, target = name, config
        if value is MISSING:
            del target[key]
        else:
            target[key] = value
    path = tmp_path / "config.yaml"
    with path.open("w") as file:
        YAML().dump(config, file)
    return path


class FakeClock:
    """Stands in for time.monotonic and time.sleep. Sleeping only moves the time."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


# --- Running from a config file -----------------------------------------------


def test_engine_runs_from_config_file_alone(tmp_path):
    """Checks REQ-01: the engine builds and steps from config.yaml alone."""
    path = write_config(
        tmp_path,
        {
            "pack.cells_per_module": 3,
            "pack.modules_in_series": 2,
            "pack.strings_in_parallel": 2,
        },
    )
    engine = start_engine(path)
    result = step(engine)

    assert len(result.strings) == 2
    assert len(result.module_cell_voltages) == 4  # 2 strings x 2 modules
    assert all(len(module) == 3 for module in result.module_cell_voltages)
    assert result.current_a == 10
    assert result.state == "DISCHARGING"
    # 6 cells in series per string, each about 3.25 V at SOC 50.
    assert result.voltage_v == pytest.approx(6 * 3.25, abs=0.05)


# --- Simulated current --------------------------------------------------------


def test_simulated_100_a_for_36000_steps_empties_100_ah(tmp_path):
    """Checks REQ-08, REQ-09: 100 A simulated for 1 h (36,000 steps) takes 100 % SOC."""
    path = write_config(tmp_path, {"start.soc": 100, "current.simulated_a": 100})
    engine = start_engine(path)

    for _ in range(18_000):
        step(engine)
    assert engine.last_result.soc_pct == pytest.approx(50, abs=1e-6)

    for _ in range(18_000):
        step(engine)
    assert engine.last_result.soc_pct == pytest.approx(0, abs=1e-6)
    assert elapsed_s(engine) == pytest.approx(3600)


def test_simulated_current_inside_deadband_changes_nothing(tmp_path):
    """Checks REQ-24: 0.4 A with a 0.5 A deadband counts as 0 A."""
    path = write_config(
        tmp_path, {"current.simulated_a": 0.4, "current.deadband_a": 0.5}
    )
    engine = start_engine(path)

    for _ in range(1000):
        result = step(engine)

    assert result.current_a == 0
    assert result.soc_pct == 50
    assert result.state == "IDLE"


# --- Config errors ------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"pack.cells_per_module": 0}, "pack.cells_per_module must be at least 1"),
        ({"pack.modules_in_series": 0}, "pack.modules_in_series must be at least 1"),
        (
            {"pack.strings_in_parallel": 0},
            "pack.strings_in_parallel must be at least 1",
        ),
        ({"pack.cells_per_module": 1.5}, "pack.cells_per_module must be a whole"),
        ({"pack.cells_per_module": MISSING}, "pack.cells_per_module is missing"),
        ({"start.soc": 150}, "start.soc must be between 0 and 100"),
        ({"start.soc": -1}, "start.soc must be between 0 and 100"),
        ({"start.temperature_c": "warm"}, "start.temperature_c must be a number"),
        ({"variation.capacity_pct": -1}, "variation.capacity_pct must be at least 0"),
        ({"variation.seed": "abc"}, "variation.seed must be a whole number"),
        ({"current.deadband_a": -0.1}, "current.deadband_a must be at least 0"),
        ({"current.timeout_ms": 0}, "current.timeout_ms must be greater than 0"),
        ({"current.simulated_a": True}, "current.simulated_a must be a number"),
        (
            {"current.source": "can"},
            "current.source 'can' is not available yet (comes with M3)",
        ),
        ({"battery": "nothing_here"}, "battery: no folder batteries/nothing_here"),
        ({"battery": MISSING}, "battery is missing"),
    ],
)
def test_config_error_names_the_setting(tmp_path, changes, message):
    """Checks REQ-01: a bad setting in config.yaml stops the start with its name."""
    path = write_config(tmp_path, changes)
    with pytest.raises(ConfigError, match=re.escape(message)):
        start_engine(path)


def test_missing_config_file_is_a_clear_error(tmp_path):
    """Checks REQ-01: without config.yaml the error names the file."""
    with pytest.raises(ConfigError, match="config file not found"):
        start_engine(tmp_path / "config.yaml")


def test_command_line_config_error_has_no_traceback(tmp_path, capsys):
    """Checks REQ-01: the command line prints one line naming the setting, exit 1."""
    path = write_config(tmp_path, {"pack.cells_per_module": 0})

    exit_code = main(["--config", str(path)])

    error_text = capsys.readouterr().err
    assert exit_code == 1
    assert "Config error: pack.cells_per_module must be at least 1" in error_text
    assert "Traceback" not in error_text


# --- The loop -----------------------------------------------------------------


@pytest.mark.parametrize("speed", [1, 10])
def test_speed_does_not_change_emulated_time_per_step(tmp_path, speed):
    """Checks REQ-09: speed only shortens the sleep, each step stays 100 ms."""
    path = write_config(tmp_path)
    engine = start_engine(path)
    fake = FakeClock()

    run(
        engine,
        speed=speed,
        max_ticks=20,
        clock=fake.clock,
        sleep=fake.sleep,
        output=lambda line: None,
    )

    assert elapsed_s(engine) == pytest.approx(20 * TICK_S)
    # 10 A for 2 s on 100 Ah: 10 * 2 / 3600 / 100 * 100 % taken out.
    assert engine.last_result.soc_pct == pytest.approx(50 - 10 * 2 / 3600)
    assert fake.sleeps == pytest.approx([TICK_S / speed] * 20)


def test_loop_keeps_fixed_deadlines(tmp_path):
    """Checks REQ-09: a late tick is followed by a shorter sleep, so time never drifts."""
    path = write_config(tmp_path)
    engine = start_engine(path)
    fake = FakeClock()

    def late_first_sleep(seconds):
        fake.sleep(seconds)
        if len(fake.sleeps) == 1:
            fake.now += 0.03  # the first sleep oversleeps by 30 ms

    run(
        engine,
        max_ticks=3,
        clock=fake.clock,
        sleep=late_first_sleep,
        output=lambda line: None,
    )

    assert fake.sleeps == pytest.approx([0.1, 0.07, 0.1])
    assert fake.now == pytest.approx(0.3)


# --- Status line --------------------------------------------------------------


def test_status_line_shows_state_current_voltage_and_soc(tmp_path):
    """Checks REQ-25: the engine prints state, current, voltage and SOC."""
    path = write_config(tmp_path)
    engine = start_engine(path)
    result = step(engine)

    line = status_line(engine, result)

    assert "DISCHARGING" in line
    assert f"I={result.current_a:7.1f} A" in line
    assert f"V={result.voltage_v:8.2f} V" in line
    assert f"SOC={result.soc_pct:6.2f} %" in line
    assert "(M1 C1)" in line


def test_loop_prints_one_status_line_per_emulated_second(tmp_path):
    """Checks REQ-25: one status line every 10 steps (1 s of emulated time)."""
    path = write_config(tmp_path)
    engine = start_engine(path)
    fake = FakeClock()
    lines = []

    run(engine, max_ticks=35, clock=fake.clock, sleep=fake.sleep, output=lines.append)

    assert len(lines) == 3
    assert lines[0].startswith("t=     1.0 s")
    assert lines[2].startswith("t=     3.0 s")
