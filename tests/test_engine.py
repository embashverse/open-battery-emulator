"""Tests for emulator/engine.py: config, one step, the loop, status line, command
line and loading interfaces.

Battery data and config.yaml are written in tmp_path by the helpers in
conftest.py, so these tests do not depend on batteries/ or the real
config.yaml. No test sleeps: the loop gets a fake clock.
"""

import re
import sys
import types
from pathlib import Path

import pytest
from conftest import MISSING, FakeClock, write_config

from emulator.engine import (
    TICK_S,
    ConfigError,
    elapsed_s,
    main,
    run,
    start_engine,
    status_line,
    step,
    stop_interfaces,
)

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


# --- Interfaces ---------------------------------------------------------------


def make_fake_interface(monkeypatch, calls, provides_current=True):
    """Put a fake interfaces.fake in sys.modules; its four functions log calls.

    The engine can then only find it the normal way: by the name "fake" under
    interfaces: in config.yaml, through importlib. start() returns
    provides_current, as the contract says.
    """

    def start(settings, pack_info):
        calls.append(("start", settings))
        return provides_current

    fake = types.ModuleType("interfaces.fake")
    fake.start = start
    fake.send = lambda result: calls.append(("send", result))
    fake.read_current = lambda: None
    fake.stop = lambda: calls.append(("stop", None))
    monkeypatch.setitem(sys.modules, "interfaces.fake", fake)
    return fake


def test_engine_loads_interface_by_name_and_calls_the_contract(tmp_path, monkeypatch):
    """Checks REQ-14: start once, send every tick, stop at the end, found by name."""
    calls = []
    make_fake_interface(monkeypatch, calls)
    path = write_config(tmp_path, {"interfaces.fake": {"enabled": True, "port": 7}})

    engine = start_engine(path)
    fake = FakeClock()
    run(engine, max_ticks=3, clock=fake.clock, sleep=fake.sleep, output=print)
    stop_interfaces(engine)

    names = [name for name, _ in calls]
    assert names == ["start", "send", "send", "send", "stop"]
    assert calls[0][1] == {"enabled": True, "port": 7}
    assert calls[3][1] is engine.last_result


def test_disabled_interface_is_not_loaded(tmp_path, monkeypatch):
    """Checks REQ-14: enabled: false means the interface is never started."""
    calls = []
    make_fake_interface(monkeypatch, calls)
    path = write_config(tmp_path, {"interfaces.fake": {"enabled": False}})

    engine = start_engine(path)

    assert engine.interfaces == []
    assert calls == []


def test_engine_source_imports_no_interface_code():
    """Checks REQ-14: engine.py never imports CAN or any interface directly."""
    source = (Path(__file__).parent.parent / "emulator" / "engine.py").read_text()
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    for line in import_lines:
        assert "interfaces" not in line
        assert "can" not in line.split()  # "import can" or "from can ..."
        assert "cantools" not in line


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"enabled": True}, "interfaces.nothing: no folder interfaces/nothing"),
        ({"enabled": "yes"}, "interfaces.nothing.enabled must be true or false"),
        ({"port": 1}, "interfaces.nothing.enabled is missing"),
    ],
)
def test_interface_config_errors_name_the_setting(tmp_path, section, message):
    """Checks REQ-14: an unknown or badly switched interface gives a clear error."""
    path = write_config(tmp_path, {"interfaces.nothing": section})
    with pytest.raises(ConfigError, match=re.escape(message)):
        start_engine(path)


def test_relative_file_paths_reach_the_interface_as_full_paths(tmp_path, monkeypatch):
    """Checks REQ-14: files next to config.yaml are passed with their full path."""
    calls = []
    make_fake_interface(monkeypatch, calls)
    (tmp_path / "messages.dbc").write_text("")
    path = write_config(
        tmp_path,
        {
            "interfaces.fake": {
                "enabled": True,
                "dbc": "messages.dbc",
                "channel": "vcan0",  # no such file: left as it is
                "nested": {"file": "messages.dbc"},
            }
        },
    )

    start_engine(path)

    settings = calls[0][1]
    assert settings["dbc"] == str((tmp_path / "messages.dbc").resolve())
    assert settings["nested"]["file"] == settings["dbc"]
    assert settings["channel"] == "vcan0"


def test_speed_above_1_with_an_interface_is_an_error(tmp_path, monkeypatch, capsys):
    """Checks REQ-26: --speed 10 with an enabled interface stops before start()."""
    calls = []
    make_fake_interface(monkeypatch, calls)
    path = write_config(tmp_path, {"interfaces.fake": {"enabled": True}})

    exit_code = main(["--config", str(path), "--speed", "10"])

    error_text = capsys.readouterr().err
    assert exit_code == 1
    assert (
        "--speed must be 1 when an interface is enabled (enabled: fake)" in error_text
    )
    assert calls == []  # nothing was started


@pytest.mark.parametrize(
    ("changes", "enabled"),
    [
        ({"current.source": "uart"}, "none"),  # no such interface at all
        (
            {"current.source": "fake", "interfaces.fake": {"enabled": False}},
            "none",
        ),
        (
            {
                "current.source": "fake",
                "interfaces.fake": {"enabled": False},
                "interfaces.other": {"enabled": True},
            },
            "other",
        ),
    ],
)
def test_current_source_must_be_an_enabled_interface(
    tmp_path, monkeypatch, changes, enabled
):
    """Checks REQ-08: current.source naming an unknown or disabled interface stops
    the start with an error naming current.source and the enabled interfaces."""
    calls = []
    fake = make_fake_interface(monkeypatch, calls)
    monkeypatch.setitem(sys.modules, "interfaces.other", fake)  # same fake, 2nd name
    path = write_config(tmp_path, changes)

    source = changes["current.source"]
    message = (
        f"current.source '{source}' must be 'simulated' or the name of an "
        f"enabled interface (enabled: {enabled})"
    )
    with pytest.raises(ConfigError, match=re.escape(message)):
        start_engine(path)
    assert calls == []  # checked before anything was started


def test_source_whose_start_returns_false_is_an_error(tmp_path, monkeypatch):
    """Checks REQ-08, REQ-14: current.source naming an interface whose start()
    returned False stops the start, and the interface is stopped again."""
    calls = []
    make_fake_interface(monkeypatch, calls, provides_current=False)
    path = write_config(
        tmp_path, {"current.source": "fake", "interfaces.fake": {"enabled": True}}
    )

    message = "current.source 'fake' cannot provide current: interfaces.fake has no"
    with pytest.raises(ConfigError, match=re.escape(message)):
        start_engine(path)
    assert [name for name, _ in calls] == ["start", "stop"]


def test_status_line_shows_timeout(tmp_path, monkeypatch):
    """Checks REQ-25, REQ-10: with an interface that sends nothing, the line ends
    with TIMEOUT; with simulated current it does not."""
    calls = []
    make_fake_interface(monkeypatch, calls)  # read_current() always None
    path = write_config(
        tmp_path, {"current.source": "fake", "interfaces.fake": {"enabled": True}}
    )
    engine = start_engine(path)
    result = step(engine)

    assert result.current_timeout
    assert status_line(engine, result).endswith("  TIMEOUT")

    simulated = start_engine(write_config(tmp_path / "sim"))
    assert "TIMEOUT" not in status_line(simulated, step(simulated))


def test_speed_above_1_without_interfaces_is_allowed(tmp_path):
    """Checks REQ-26: without interfaces any speed is fine."""
    path = write_config(tmp_path, {"interfaces.can.enabled": False})
    engine = start_engine(path, speed=10)
    assert engine.interfaces == []
