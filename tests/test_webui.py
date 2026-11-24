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
    """Checks REQ-34: webui.start() gets an EngineHandle with exactly two
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
    assert [f.name for f in fields(handle)] == ["read_state", "send_command"]
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
