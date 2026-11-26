"""Tests for the web UI: starting it from the engine, the API, commands and the page.

The server is started on port 0, so the system picks a free port, and called
with urllib from the standard library. Every test stops the engine at the end,
so no server thread is left running.
"""

import json
import os
import signal
import sys
import threading
import types
import urllib.error
import urllib.request
from dataclasses import asdict, fields
from pathlib import Path

import pytest
from conftest import write_config

from emulator.engine import (
    Engine,
    EngineHandle,
    main,
    start_engine,
    step,
    stop_engine,
)

WEBUI_ON = {"webui": {"enabled": True, "host": "127.0.0.1", "port": 0}}
STATIC_FOLDER = Path(__file__).parent.parent / "webui" / "static"


# --- Helpers ------------------------------------------------------------------


def url(engine, path):
    return f"http://127.0.0.1:{engine.webui_port}{path}"


def get_json(engine, path):
    with urllib.request.urlopen(url(engine, path), timeout=5) as response:
        return json.load(response)


def post(engine, body):
    """POST body (a dict, or raw bytes) to /api/command. Returns (status, json)."""
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    request = urllib.request.Request(
        url(engine, "/api/command"),
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, json.load(error)


def webui_threads():
    return [t for t in threading.enumerate() if t.name == "webui" and t.is_alive()]


@pytest.fixture
def engine(tmp_path):
    """An engine with the web UI on, stopped after the test."""
    started = start_engine(write_config(tmp_path, WEBUI_ON))
    yield started
    stop_engine(started)


def add_fake_interface(monkeypatch, name, provides_current):
    fake = types.ModuleType(f"interfaces.{name}")
    fake.start = lambda settings, pack_info: provides_current
    fake.send = lambda result: None
    fake.read_current = lambda: None
    fake.stop = lambda: None
    monkeypatch.setitem(sys.modules, f"interfaces.{name}", fake)
    return fake


# --- Starting the web UI ------------------------------------------------------


def test_webui_disabled_starts_no_server_or_thread(tmp_path, monkeypatch):
    """Checks REQ-15: with webui.enabled false the web UI is not even imported."""
    for name in ("webui", "webui.server"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    path = write_config(tmp_path, {"webui": {"enabled": False, "port": 0}})

    engine = start_engine(path)
    step(engine)
    stop_engine(engine)

    assert engine.webui is None
    assert engine.webui_port is None
    assert webui_threads() == []
    assert "webui" not in sys.modules


def test_engine_gives_the_webui_only_a_handle(tmp_path, monkeypatch):
    """Checks REQ-34: webui.start() gets an EngineHandle with exactly three
    functions, never the Engine, and the state it reads is a copy."""
    received = []
    fake = types.ModuleType("webui")
    fake.start = lambda host, port, handle: received.append(handle) or 1234
    fake.stop = lambda: None
    monkeypatch.setitem(sys.modules, "webui", fake)

    engine = start_engine(write_config(tmp_path, WEBUI_ON))
    step(engine)
    (handle,) = received

    assert isinstance(handle, EngineHandle)
    assert not isinstance(handle, Engine)
    assert [f.name for f in fields(handle)] == [
        "read_state",
        "send_command",
        "read_config",
    ]
    state = handle.read_state()
    state["result"]["soc_pct"] = -1  # changing the copy must not reach the engine
    state["sources"].append("other")
    assert engine.live_state["result"]["soc_pct"] != -1
    assert engine.live_state["sources"] == ["simulated"]
    stop_engine(engine)


def test_engine_source_has_no_static_webui_import():
    """Checks REQ-34: engine.py loads webui only by name, with importlib."""
    source = (Path(__file__).parent.parent / "emulator" / "engine.py").read_text()
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    assert not [line for line in import_lines if "webui" in line]


# --- Reading the state --------------------------------------------------------


def test_api_state_matches_the_latest_engine_result(engine):
    """Checks REQ-15: GET /api/state is the latest result plus live settings."""
    for _ in range(3):
        step(engine)

    state = get_json(engine, "/api/state")

    assert state["result"] == json.loads(json.dumps(asdict(engine.last_result)))
    assert state["elapsed_s"] == pytest.approx(0.3)
    assert state["current_source"] == "simulated"
    assert state["simulated_a"] == 10
    assert state["hold_soc"] is False
    assert state["sources"] == ["simulated"]


def test_api_state_before_the_first_tick_has_no_result(engine):
    """Checks REQ-15: before any tick the page gets result null, not an error."""
    assert get_json(engine, "/api/state")["result"] is None


def test_page_and_its_files_are_served(engine):
    """Checks REQ-15: GET / serves the page, which loads style.css and app.js."""
    with urllib.request.urlopen(url(engine, "/"), timeout=5) as response:
        page = response.read().decode()
    assert "Battery Emulator" in page
    for path in ("/style.css", "/app.js"):
        with urllib.request.urlopen(url(engine, path), timeout=5) as response:
            assert response.status == 200


# --- Commands -----------------------------------------------------------------


def test_command_is_applied_at_the_next_step_not_at_once(engine):
    """Checks REQ-34: a POSTed command waits in the queue until step()."""
    step(engine)
    soc_before = engine.last_result.soc_pct

    assert post(engine, {"command": "set_soc", "value": 20})[0] == 200
    assert post(engine, {"command": "hold_soc", "value": True})[0] == 200
    assert post(engine, {"command": "set_simulated_current", "value": -5})[0] == 200

    # Nothing changed yet: only the engine thread changes the pack.
    assert engine.pack.hold_soc is False
    assert engine.simulated_a == 10
    assert get_json(engine, "/api/state")["result"]["soc_pct"] == soc_before

    result = step(engine)

    assert result.soc_pct == pytest.approx(20)  # held, so exactly 20
    assert result.current_a == -5
    state = get_json(engine, "/api/state")
    assert state["hold_soc"] is True
    assert state["simulated_a"] == -5


def test_set_source_switches_at_the_next_step(tmp_path, monkeypatch):
    """Checks REQ-34, REQ-08: switching to an interface that provides current
    happens at the next step and starts in timeout, like at start (REQ-10)."""
    add_fake_interface(monkeypatch, "fake", provides_current=True)
    path = write_config(tmp_path, {**WEBUI_ON, "interfaces.fake": {"enabled": True}})
    engine = start_engine(path)
    try:
        assert get_json(engine, "/api/state")["sources"] == ["simulated", "fake"]
        assert post(engine, {"command": "set_source", "value": "fake"})[0] == 200
        assert engine.current_source == "simulated"

        result = step(engine)

        assert engine.current_source == "fake"
        assert result.current_timeout is True
        assert result.current_a == 0
    finally:
        stop_engine(engine)


@pytest.mark.parametrize(
    "body, message",
    [
        ({"command": "explode", "value": 1}, "unknown command 'explode'"),
        ({"value": 1}, "unknown command"),
        ({"command": "set_soc", "value": -1}, "SOC must be between 0 and 100"),
        ({"command": "set_soc", "value": 101}, "SOC must be between 0 and 100"),
        ({"command": "set_soc", "value": "abc"}, "value must be a number"),
        ({"command": "set_simulated_current", "value": True}, "must be a number"),
        ({"command": "hold_soc", "value": "yes"}, "true or false"),
        ({"command": "set_source", "value": "uart"}, "source 'uart' must be"),
        ([1, 2], "must be an object"),
        (b"not json", "not valid JSON"),
        (b'{"command": "set_simulated_current", "value": NaN}', "finite number"),
    ],
)
def test_invalid_command_returns_400_with_a_message(engine, body, message):
    """Checks REQ-15, REQ-21: a bad command is refused at once with a clear
    message, and nothing is queued."""
    status, answer = post(engine, body)

    assert status == 400
    assert message in answer["error"]
    assert engine.commands.empty()


def test_source_that_cannot_provide_current_returns_400(tmp_path, monkeypatch):
    """Checks REQ-08, REQ-34: an enabled interface whose start() returned False
    cannot be chosen as the source."""
    add_fake_interface(monkeypatch, "listener", provides_current=False)
    path = write_config(
        tmp_path, {**WEBUI_ON, "interfaces.listener": {"enabled": True}}
    )
    engine = start_engine(path)
    try:
        status, answer = post(engine, {"command": "set_source", "value": "listener"})

        assert status == 400
        assert "source 'listener'" in answer["error"]
        assert "available: simulated" in answer["error"]
        assert get_json(engine, "/api/state")["sources"] == ["simulated"]
    finally:
        stop_engine(engine)


def test_state_lists_every_interface_with_availability_and_reason(
    tmp_path, monkeypatch
):
    """Checks REQ-15, REQ-18: the state lists simulated and every interface in
    config.yaml, in file order, each with whether it can provide current now
    and why not, so the page can show them without knowing any by name."""
    add_fake_interface(monkeypatch, "withinput", provides_current=True)
    add_fake_interface(monkeypatch, "noinput", provides_current=False)
    path = write_config(
        tmp_path,
        {
            **WEBUI_ON,
            "start.autostart": False,
            "interfaces.withinput": {"enabled": True},
            "interfaces.noinput": {"enabled": True},
            "interfaces.off": {"enabled": False},  # no folder: never loaded
        },
    )
    engine = start_engine(path)
    try:
        assert get_json(engine, "/api/state")["source_options"] == [
            {"name": "simulated", "available": True, "reason": None},
            {"name": "withinput", "available": False, "reason": "starts at Play"},
            {"name": "noinput", "available": False, "reason": "starts at Play"},
            {"name": "off", "available": False, "reason": "disabled in Config"},
        ]

        assert post(engine, {"command": "play", "value": None})[0] == 200
        step(engine)
        state = get_json(engine, "/api/state")

        assert state["source_options"] == [
            {"name": "simulated", "available": True, "reason": None},
            {"name": "withinput", "available": True, "reason": None},
            {"name": "noinput", "available": False, "reason": "has no current input"},
            {"name": "off", "available": False, "reason": "disabled in Config"},
        ]
        assert state["sources"] == ["simulated", "withinput"]
        # Used by the page for the Timeout tip and the cell grid colours.
        assert state["timeout_ms"] == 500
        assert (state["cell_v_min"], state["cell_v_max"]) == (2.5, 3.65)
    finally:
        stop_engine(engine)


def test_state_has_the_soc_of_every_cell(tmp_path):
    """Checks REQ-15: the state has each cell's SOC, module by module and
    string by string, in the same shape as the cell voltages, for the cell
    tooltip on the page."""
    path = write_config(
        tmp_path,
        {
            **WEBUI_ON,
            "pack": {
                "cells_per_module": 3,
                "modules_in_series": 2,
                "strings_in_parallel": 2,
            },
            "variation.soc_pct": 5,  # every cell gets its own SOC
        },
    )
    engine = start_engine(path)
    try:
        for _ in range(3):
            step(engine)
        state = get_json(engine, "/api/state")

        expected = [
            [cell.soc_pct for cell in module.cells]
            for string in engine.pack.strings
            for module in string.modules
        ]
        got = [soc for module in state["module_cell_socs"] for soc in module]
        want = [soc for module in expected for soc in module]
        assert got == pytest.approx(want)
        assert len(set(want)) == 12  # really one value per cell
        voltages = state["result"]["module_cell_voltages"]
        assert [len(m) for m in state["module_cell_socs"]] == [len(m) for m in voltages]
    finally:
        stop_engine(engine)


def test_state_has_the_pack_capacity(tmp_path):
    """Checks REQ-15: the state has the pack capacity in Ah (strings x the
    capacity in cell.csv), for the time to empty or full on the page. None
    before the first Play."""
    path = write_config(
        tmp_path,
        {
            **WEBUI_ON,
            "pack": {
                "cells_per_module": 2,
                "modules_in_series": 1,
                "strings_in_parallel": 2,
            },
            "start.autostart": False,
        },
    )
    engine = start_engine(path)
    try:
        assert get_json(engine, "/api/state")["capacity_ah"] is None
        assert post(engine, {"command": "play", "value": None})[0] == 200
        step(engine)
        state = get_json(engine, "/api/state")
        assert state["capacity_ah"] == pytest.approx(2 * 100)  # test battery: 100 Ah
    finally:
        stop_engine(engine)


# --- Config tab ---------------------------------------------------------------


def test_config_form_and_save_through_the_api(tmp_path):
    """Checks REQ-16, REQ-34: GET /api/config gives the form built from
    config.yaml; a Save posted as a command is applied by the engine at its
    next pass, and its result shows up in /api/state under its id."""
    path = write_config(tmp_path, {**WEBUI_ON, "start.autostart": False})
    engine = start_engine(path)
    try:
        form = get_json(engine, "/api/config")
        paths = [field["path"] for field in form["fields"]]
        assert "pack.cells_per_module" in paths
        assert [b["name"] for b in form["batteries"]] == ["test"]
        assert form["restart_needed"] == ["webui.host", "webui.port"]

        status, answer = post(
            engine,
            {
                "command": "save_config",
                "value": {"version": form["version"], "changes": {"start.soc": 30}},
            },
        )
        assert status == 200
        assert "soc: 50" in path.read_text()  # not yet: the engine applies it

        step(engine)

        actions = get_json(engine, "/api/state")["actions"]
        assert [a["id"] for a in actions if a["ok"]] == [answer["id"]]
        assert "soc: 30" in path.read_text()
        assert get_json(engine, "/api/config")["version"] != form["version"]
    finally:
        stop_engine(engine)


def test_live_commands_are_refused_while_stopped(tmp_path):
    """Checks REQ-36: Set SOC and the other live controls need RUNNING."""
    engine = start_engine(
        write_config(tmp_path, {**WEBUI_ON, "start.autostart": False})
    )
    try:
        status, answer = post(engine, {"command": "set_soc", "value": 50})
        assert status == 400
        assert "only while RUNNING" in answer["error"]
    finally:
        stop_engine(engine)


# --- Stopping -----------------------------------------------------------------


def test_server_stops_when_the_engine_stops(tmp_path, monkeypatch, capsys):
    """Checks REQ-15, REQ-33: SIGTERM ends main() and the web server with it:
    the thread is gone and the port no longer answers."""
    fake = add_fake_interface(monkeypatch, "fake", provides_current=True)
    ports = []

    def send_then_sigterm(result):
        ports.append(int(capsys.readouterr().out.split(":")[-1]))
        os.kill(os.getpid(), signal.SIGTERM)  # as if systemd stopped the service

    fake.send = send_then_sigterm
    path = write_config(tmp_path, {**WEBUI_ON, "interfaces.fake": {"enabled": True}})

    assert main(["--config", str(path)]) == 0

    assert webui_threads() == []
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(f"http://127.0.0.1:{ports[0]}/api/state", timeout=2)


# --- The page -----------------------------------------------------------------


def test_page_loads_nothing_from_the_internet():
    """Checks REQ-35: no http:// or https:// in any page file, so every script,
    style and font comes from this server."""
    files = list(STATIC_FOLDER.iterdir())
    assert {f.name for f in files} >= {"index.html", "style.css", "app.js"}
    for file in files:
        text = file.read_text().lower()
        assert "http://" not in text, file.name
        assert "https://" not in text, file.name


def test_page_names_no_interface():
    """Checks REQ-18: app.js names no interface, so every section under
    interfaces: in config.yaml gets its own Config tab without changing the page."""
    text = (STATIC_FOLDER / "app.js").read_text()
    folder = Path(__file__).parent.parent / "interfaces"
    names = [
        p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith("_")
    ]
    assert names
    for name in names:
        for written in (f'"{name}"', f"'{name}'", f"interfaces.{name}"):
            assert written not in text, written
