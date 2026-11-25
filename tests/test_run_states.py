"""Tests for the run states STOPPED and RUNNING: Play, Stop and autostart.

Commands are queued with queue_command, exactly as the web UI's handle does,
and the loop runs with a fake clock, so no test waits.
"""

import sys
import types

import pytest
from conftest import FakeClock, write_config

from emulator.config import ConfigError
from emulator.engine import (
    RUNNING,
    STOPPED,
    queue_command,
    run,
    start_engine,
    stop_engine,
)

STOPPED_AT_START = {"start.autostart": False}


# --- Helpers ------------------------------------------------------------------


def add_recording_interface(monkeypatch, calls, start_error=None):
    """A fake interfaces.fake that logs every contract call by name."""

    def start(settings, pack_info):
        calls.append("start")
        if start_error is not None:
            raise start_error
        return True

    fake = types.ModuleType("interfaces.fake")
    fake.start = start
    fake.send = lambda result: calls.append("send")
    fake.read_current = lambda: None
    fake.stop = lambda: calls.append("stop")
    monkeypatch.setitem(sys.modules, "interfaces.fake", fake)


def run_passes(engine, passes):
    """Run the loop for a number of passes without waiting or printing."""
    fake = FakeClock()
    run(engine, max_ticks=passes, clock=fake.clock, sleep=fake.sleep, output=print)


# --- Play and Stop ------------------------------------------------------------


def test_play_starts_ticks_and_interfaces_stop_stops_them(tmp_path, monkeypatch):
    """Checks REQ-36: while STOPPED nothing ticks and nothing is sent; Play
    starts the interfaces and the ticks; Stop stops the interfaces, and no
    frame is sent while STOPPED. The loop keeps running all the time."""
    calls = []
    add_recording_interface(monkeypatch, calls)
    path = write_config(
        tmp_path, {**STOPPED_AT_START, "interfaces.fake": {"enabled": True}}
    )
    engine = start_engine(path)

    run_passes(engine, 5)
    assert engine.run_state == STOPPED
    assert engine.tick_count == 0
    assert calls == []  # not even started

    queue_command(engine, {"command": "play"})
    run_passes(engine, 3)  # the first pass applies Play, then ticks
    assert engine.run_state == RUNNING
    assert engine.tick_count == 3
    assert calls == ["start", "send", "send", "send"]

    queue_command(engine, {"command": "stop"})
    run_passes(engine, 5)
    assert engine.run_state == STOPPED
    assert engine.tick_count == 3  # no ticks while STOPPED
    assert calls == ["start", "send", "send", "send", "stop"]  # no more frames
    assert [(a["command"], a["ok"]) for a in engine.live_state["actions"]] == [
        ("play", True),
        ("stop", True),
    ]
    stop_engine(engine)


def test_play_rebuilds_the_pack_from_the_saved_file(tmp_path):
    """Checks REQ-36: Play reads config.yaml again, so a changed file is used
    at the next Play."""
    path = write_config(tmp_path, STOPPED_AT_START)
    engine = start_engine(path)
    queue_command(engine, {"command": "play"})
    run_passes(engine, 1)
    assert len(engine.last_result.module_cell_voltages[0]) == 1

    queue_command(engine, {"command": "stop"})
    run_passes(engine, 1)
    path.write_text(
        path.read_text().replace("cells_per_module: 1", "cells_per_module: 4")
    )
    queue_command(engine, {"command": "play"})
    run_passes(engine, 1)

    assert len(engine.last_result.module_cell_voltages[0]) == 4
    assert engine.tick_count == 1  # time starts over
    stop_engine(engine)


@pytest.mark.parametrize(
    ("changes", "start_error", "message"),
    [
        ({"battery": "missing"}, None, "battery: no folder batteries/missing"),
        (
            {"interfaces.fake": {"enabled": True}},
            ValueError("interfaces.fake.dbc: broken file"),
            "interfaces.fake.dbc: broken file",
        ),
    ],
)
def test_play_that_fails_stays_stopped_with_the_error(
    tmp_path, monkeypatch, changes, start_error, message
):
    """Checks REQ-36: if Play fails (a bad setting, or an interface refusing to
    start), the emulator stays STOPPED and the error reaches the page."""
    calls = []
    add_recording_interface(monkeypatch, calls, start_error=start_error)
    engine = start_engine(write_config(tmp_path, {**STOPPED_AT_START, **changes}))

    command_id = queue_command(engine, {"command": "play"})
    run_passes(engine, 3)

    assert engine.run_state == STOPPED
    assert engine.tick_count == 0
    assert engine.live_state["run_state"] == STOPPED
    assert engine.live_state["error"] == message
    (action,) = engine.live_state["actions"]
    assert action == {
        "id": command_id,
        "command": "play",
        "ok": False,
        "message": message,
    }
    assert "send" not in calls
    stop_engine(engine)


def test_boot_error_with_the_webui_on_stays_stopped(tmp_path):
    """Checks REQ-36, REQ-37: with autostart true and a bad setting, the web UI
    still starts and the emulator stays STOPPED with the error, so it can be
    fixed in the Config tab."""
    path = write_config(
        tmp_path,
        {
            "battery": "missing",
            "webui": {"enabled": True, "host": "127.0.0.1", "port": 0},
        },
    )
    engine = start_engine(path)
    try:
        assert engine.run_state == STOPPED
        assert engine.webui is not None
        assert engine.live_state["error"] == "battery: no folder batteries/missing"
    finally:
        stop_engine(engine)


def test_boot_error_with_the_webui_off_is_raised(tmp_path):
    """Checks REQ-37: with the web UI off nothing could fix a bad setting, so
    the start stops with the error, as before."""
    with pytest.raises(ConfigError, match="battery: no folder batteries/missing"):
        start_engine(write_config(tmp_path, {"battery": "missing"}))


# --- autostart ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("autostart", "state"),
    [(True, RUNNING), (False, STOPPED), (None, RUNNING)],  # None: left out
)
def test_autostart_sets_the_run_state_at_start(tmp_path, monkeypatch, autostart, state):
    """Checks REQ-37: autostart true (or missing) starts RUNNING with the
    interfaces started, false starts STOPPED with nothing started."""
    calls = []
    add_recording_interface(monkeypatch, calls)
    changes = {"interfaces.fake": {"enabled": True}}
    if autostart is not None:
        changes["start.autostart"] = autostart
    path = write_config(tmp_path, changes)

    engine = start_engine(path)

    assert engine.run_state == state
    assert engine.live_state["run_state"] == state
    assert calls == (["start"] if state == RUNNING else [])
    stop_engine(engine)


def test_autostart_must_be_true_or_false(tmp_path):
    """Checks REQ-37: a wrong value names the setting."""
    with pytest.raises(ConfigError, match="start.autostart must be true or false"):
        start_engine(write_config(tmp_path, {"start.autostart": "yes"}))
