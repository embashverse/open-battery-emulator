"""Tests for emulator/config.py: the settings form, the battery list and Save.

Save is a queued command, applied by the engine thread, so most tests queue it
with queue_command (as the web UI's handle does) and run one step().
"""

import shutil
from pathlib import Path

import pytest
from conftest import write_battery, write_config

from emulator.config import (
    ConfigError,
    file_version,
    list_batteries,
    read_config_file,
    read_form,
)
from emulator.engine import (
    CommandError,
    queue_command,
    save_settings,
    start_engine,
    step,
    stop_engine,
)

REPO = Path(__file__).parent.parent
STOPPED_AT_START = {"start.autostart": False}


# --- Helpers ------------------------------------------------------------------


def save(engine, changes, version=None):
    """Queue a Save of these changes and apply it. Returns the action result."""
    if version is None:
        version = read_form(engine.config_path)["version"]
    command_id = queue_command(
        engine,
        {"command": "save_config", "value": {"version": version, "changes": changes}},
    )
    step(engine)
    (action,) = [a for a in engine.live_state["actions"] if a["id"] == command_id]
    return action


def fields_by_path(form):
    return {field["path"]: field for field in form["fields"]}


# --- Save: only while STOPPED -------------------------------------------------


def test_save_is_refused_while_running(tmp_path):
    """Checks REQ-16: settings are saved only while STOPPED. The web thread
    refuses at once, and the engine thread checks again when it applies."""
    path = write_config(tmp_path)  # autostart: RUNNING
    engine = start_engine(path)
    before = path.read_bytes()
    value = {"version": read_form(path)["version"], "changes": {"start.soc": 20}}

    with pytest.raises(CommandError, match="press Stop before saving"):
        queue_command(engine, {"command": "save_config", "value": value})
    with pytest.raises(CommandError, match="press Stop before saving"):
        save_settings(engine, value)
    assert path.read_bytes() == before
    stop_engine(engine)


# --- Save: comments, order and the temporary file -----------------------------


def test_save_keeps_comments_and_key_order_and_leaves_no_temp_file(tmp_path):
    """Checks REQ-16: Save on a copy of the real config.yaml changes only the
    lines of the changed settings; comments, order and hex numbers stay. The
    temporary file is gone afterwards."""
    shutil.copytree(
        REPO / "batteries" / "lfp_100ah", tmp_path / "batteries" / "lfp_100ah"
    )
    original = (REPO / "config.yaml").read_text()
    original = original.replace("autostart: true ", "autostart: false")
    # The shipped web UI is on; the test must not open a real port.
    original = original.replace(
        "  enabled: true\n  host: 0.0.0.0", "  enabled: false\n  host: 0.0.0.0"
    )
    path = tmp_path / "config.yaml"
    path.write_text(original)
    engine = start_engine(path)

    action = save(
        engine,
        {
            "pack.cells_per_module": 10,
            "interfaces.can.module_frames.base_id": 0x300,
            "start.soc": 80,  # unchanged: left exactly as written
        },
    )

    assert action["ok"], action["message"]
    old_lines = original.splitlines()
    new_lines = path.read_text().splitlines()
    assert len(new_lines) == len(old_lines)
    changed = [(old, new) for old, new in zip(old_lines, new_lines) if old != new]
    assert changed == [
        ("  cells_per_module: 12", "  cells_per_module: 10"),
        ("      base_id: 0x200", "      base_id: 0x300"),
    ]
    files = sorted(p.name for p in tmp_path.iterdir())
    assert files == ["batteries", "config.yaml"]  # no .config.yaml.*.tmp left
    stop_engine(engine)


# --- Save: the same checks as at start ----------------------------------------


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("pack.cells_per_module", 0),
        ("start.soc", 150),
        ("current.source", "uart"),
        ("battery", "missing"),
    ],
)
def test_invalid_value_gets_the_message_from_start_and_file_is_unchanged(
    tmp_path, setting, value
):
    """Checks REQ-16: Save checks with the same code as the start, so a bad
    value gives exactly the message the engine gives at start, and
    config.yaml is not touched."""
    path = write_config(tmp_path / "edit", STOPPED_AT_START)
    engine = start_engine(path)
    before = path.read_bytes()

    action = save(engine, {setting: value})

    with pytest.raises(ConfigError) as at_start:
        start_engine(write_config(tmp_path / "start", {setting: value}))
    assert action["ok"] is False
    assert action["message"] == str(at_start.value)
    assert path.read_bytes() == before
    stop_engine(engine)


def test_unknown_setting_is_refused(tmp_path):
    """Checks REQ-16: Save only changes settings that are in config.yaml."""
    path = write_config(tmp_path, STOPPED_AT_START)
    engine = start_engine(path)

    action = save(engine, {"pack.colour": "red"})

    assert action["ok"] is False
    assert action["message"] == "pack.colour: not a setting in config.yaml"
    stop_engine(engine)


def test_host_and_port_say_a_restart_is_needed(tmp_path):
    """Checks REQ-15: webui.host and webui.port are saved, and the answer says
    they need a restart of the program."""
    path = write_config(tmp_path, STOPPED_AT_START)
    engine = start_engine(path)

    action = save(engine, {"webui.port": 9090})

    assert action["ok"], action["message"]
    assert "after restarting the program" in action["message"]
    assert "port: 9090" in path.read_text()
    stop_engine(engine)


# --- Save: the file changed since the page loaded it --------------------------


def test_save_is_refused_when_the_file_changed_since_it_was_loaded(tmp_path):
    """Checks REQ-17: the page sends the version it loaded; if config.yaml was
    edited since (even a comment), Save is refused and the page can reload."""
    path = write_config(tmp_path, STOPPED_AT_START)
    engine = start_engine(path)
    loaded = read_form(path)["version"]
    path.write_text(path.read_text() + "# edited by hand\n")
    edited = path.read_bytes()

    action = save(engine, {"start.soc": 20}, version=loaded)

    assert action["ok"] is False
    assert "changed since the page loaded it" in action["message"]
    assert path.read_bytes() == edited
    assert read_form(path)["version"] != loaded
    assert read_form(path)["version"] == file_version(path.read_text())
    stop_engine(engine)


# --- What the form is built from ----------------------------------------------


def test_battery_list_comes_from_the_folders_in_batteries(tmp_path):
    """Checks REQ-18: every folder in batteries/ is a choice, sorted; files
    there are not."""
    path = write_config(tmp_path)  # writes batteries/test
    write_battery(tmp_path, "another")
    (tmp_path / "batteries" / "notes.txt").write_text("not a battery")

    batteries = list_batteries(tmp_path)

    assert [b["name"] for b in batteries] == ["another", "test"]
    assert batteries[0]["capacity_ah"] == 100
    assert batteries[0]["nominal_v"] == pytest.approx(3.25)  # discharge OCV at 50 %
    assert read_form(path)["batteries"] == batteries


def test_new_interface_section_appears_in_the_form_without_code_changes(tmp_path):
    """Checks REQ-18: a section under interfaces: that no code knows about is
    in the form data with all its settings, and can be saved."""
    section = {
        "enabled": False,
        "port": "/dev/ttyUSB0",
        "baudrate": 115200,
        "framing": {"frame_id": 0x42, "stop_bits": 1.5},
    }
    path = write_config(tmp_path, {**STOPPED_AT_START, "interfaces.uart": section})

    fields = fields_by_path(read_form(path))

    assert fields["interfaces.uart.enabled"] == {
        "path": "interfaces.uart.enabled",
        "value": False,
        "type": "bool",
    }
    assert fields["interfaces.uart.port"]["type"] == "str"
    assert fields["interfaces.uart.baudrate"]["value"] == 115200
    assert fields["interfaces.uart.framing.stop_bits"]["type"] == "float"

    engine = start_engine(path)
    action = save(engine, {"interfaces.uart.baudrate": 9600})
    assert action["ok"], action["message"]
    assert fields_by_path(read_form(path))["interfaces.uart.baudrate"]["value"] == 9600
    stop_engine(engine)


def test_form_shows_autostart_even_if_the_file_has_none(tmp_path):
    """Checks REQ-37: autostart is shown with its default when missing."""
    path = write_config(tmp_path)
    assert fields_by_path(read_form(path))["start.autostart"]["value"] is True


# --- The shipped config.yaml --------------------------------------------------


def test_shipped_config_enables_nothing_that_needs_hardware():
    """Checks REQ-44: the config.yaml in the repo uses simulated current, has CAN
    and UART disabled and the web UI on, so a fresh install runs without errors."""
    raw = read_config_file(REPO / "config.yaml")

    assert raw["current"]["source"] == "simulated"
    assert raw["interfaces"]["can"]["enabled"] is False
    assert raw["interfaces"]["uart"]["enabled"] is False
    assert raw["webui"]["enabled"] is True
