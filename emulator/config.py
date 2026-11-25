"""High level: everything about config.yaml.

Reads the file, checks every setting, builds the settings form for the web UI,
lists the battery types and saves changes back to the file. Only engine.py
imports this file; it uses only pack.py.

Saving keeps comments and key order (ruamel.yaml round trip) and replaces the
file in one step, so a crash never leaves half a file (REQ-16). Every save
checks that the file did not change since the page loaded it (REQ-17).
"""

import hashlib
import io
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.scalarint import HexInt

from emulator.pack import PackSettings, battery_summary

# --- 1. Constants -------------------------------------------------------------

WEBUI_DEFAULT_HOST = "0.0.0.0"  # reachable from other devices on the network
WEBUI_DEFAULT_PORT = 8080
AUTOSTART_DEFAULT = True  # start RUNNING, so the Pi service is on the bus at boot
# Settings that may be missing from the file. The form shows them with these
# values, and Save may add them.
OPTIONAL_SETTINGS = {
    "start.autostart": AUTOSTART_DEFAULT,
    "webui.enabled": False,
    "webui.host": WEBUI_DEFAULT_HOST,
    "webui.port": WEBUI_DEFAULT_PORT,
}
# Read once when the program starts, so a change needs a restart.
RESTART_NEEDED = ("webui.host", "webui.port")


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
    autostart: bool = AUTOSTART_DEFAULT
    webui_enabled: bool = False
    webui_host: str = WEBUI_DEFAULT_HOST
    webui_port: int = WEBUI_DEFAULT_PORT


# --- 3. Reading config.yaml ---------------------------------------------------


def make_yaml() -> YAML:
    """Round trip mode ("rt") keeps comments and key order when saving.
    Reading uses the same mode, so a saved file is the file that was read."""
    return YAML(typ="rt")


def read_config_file(path: Path) -> dict:
    """Load config.yaml as nested dictionaries."""
    return parse_config_text(read_config_text(path), path)


def read_config_text(path: Path) -> str:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    return path.read_text()


def parse_config_text(text: str, path: Path) -> dict:
    try:
        raw = make_yaml().load(text)
    except YAMLError as error:
        raise ConfigError(f"{path} is not valid YAML: {error}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain settings as 'name: value' lines")
    return raw


def file_version(text: str) -> str:
    """A short fingerprint of the file's text (REQ-17). Any change to the
    file, even a comment, gives a different version."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- 4. Checking config values ------------------------------------------------
# Every error names the setting, for example "pack.cells_per_module".
# Play and Save both use these same functions (REQ-16).


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


def check_autostart(raw: dict) -> bool:
    """start.autostart: true starts RUNNING, false starts STOPPED (REQ-37).
    Missing means true. A missing start section is reported by check_config."""
    section = raw.get("start")
    if not isinstance(section, dict):
        return AUTOSTART_DEFAULT
    value = section.get("autostart", AUTOSTART_DEFAULT)
    if not isinstance(value, bool):
        raise ConfigError("start.autostart must be true or false")
    return value


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

    Nothing here knows what any setting means. It only makes relative file
    paths full (see resolve_paths), so interfaces get usable paths.
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


def check_speed(interfaces: list[tuple[str, dict]], speed: float) -> None:
    """REQ-26: with a device on the other end, time must be real time."""
    if interfaces and speed != 1:
        names = ", ".join(name for name, _ in interfaces)
        raise ConfigError(
            f"--speed must be 1 when an interface is enabled (enabled: {names})"
        )


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
        autostart=check_autostart(raw),
        webui_enabled=webui_enabled,
        webui_host=webui_host,
        webui_port=webui_port,
    )


# --- 5. Settings form for the web UI ------------------------------------------
# The form is built from whatever is in the file. Nothing here knows what an
# interface setting means, so a new section under interfaces: shows up in the
# Config tab without any code change (REQ-18).


def read_form(config_path: Path) -> dict:
    """Everything the Config tab needs, as plain values (ready for JSON).

    version  fingerprint of the file, sent back with Save (REQ-17)
    fields   every setting as {"path", "value", "type"}, in file order
    batteries  the battery types in batteries/, with capacity and nominal voltage
    restart_needed  settings that only take effect after a restart
    """
    config_path = Path(config_path)
    text = read_config_text(config_path)
    raw = parse_config_text(text, config_path)
    fields = flatten(raw, "")
    known = {field["path"] for field in fields}
    for path, value in OPTIONAL_SETTINGS.items():
        if path not in known:
            fields.append(make_field(path, value))
    return {
        "version": file_version(text),
        "fields": fields,
        "batteries": list_batteries(config_path.parent),
        "restart_needed": list(RESTART_NEEDED),
    }


def flatten(section: dict, prefix: str) -> list[dict]:
    """One field per setting. Nested sections become dotted paths, like
    interfaces.can.module_frames.base_id."""
    fields = []
    for key, value in section.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            fields.extend(flatten(value, path + "."))
        else:
            fields.append(make_field(path, value))
    return fields


def make_field(path: str, value) -> dict:
    """The field for one setting. The type tells the page which input to show:
    bool = checkbox, int / float = number, hex = number shown as 0x..,
    str = text, other (empty, lists) = shown but not editable."""
    # bool first: True is also an int in Python. HexInt before int: it is one.
    if isinstance(value, bool):
        return {"path": path, "value": value, "type": "bool"}
    if isinstance(value, HexInt):
        return {"path": path, "value": int(value), "type": "hex"}
    if isinstance(value, int):
        return {"path": path, "value": int(value), "type": "int"}
    if isinstance(value, float):
        return {"path": path, "value": float(value), "type": "float"}
    if isinstance(value, str):
        return {"path": path, "value": str(value), "type": "str"}
    return {
        "path": path,
        "value": None if value is None else str(value),
        "type": "other",
    }


# --- 6. Battery list ----------------------------------------------------------


def list_batteries(config_folder: Path) -> list[dict]:
    """Every folder in batteries/ is a battery type (REQ-18), sorted by name.

    Capacity and nominal voltage are None if the folder does not load; the
    error itself is shown when that battery is used.
    """
    folder = Path(config_folder) / "batteries"
    if not folder.is_dir():
        return []
    batteries = []
    for entry in sorted(folder.iterdir()):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        try:
            summary = battery_summary(entry)
            capacity_ah, nominal_v = summary.capacity_ah, summary.nominal_v
        except (ValueError, OSError):
            capacity_ah, nominal_v = None, None
        batteries.append(
            {"name": entry.name, "capacity_ah": capacity_ah, "nominal_v": nominal_v}
        )
    return batteries


# --- 7. Saving ----------------------------------------------------------------
# In two steps, so the engine can run its start checks on the changed settings
# before anything is written: apply_changes, then (engine) checks, then
# write_config_file.


def apply_changes(config_path: Path, version: str, changes: dict) -> dict:
    """The file's settings with the changes applied, not yet written.

    changes: {"pack.cells_per_module": 10, ...}. Refused if the file changed
    since the page loaded it (REQ-17), or a path is not a setting.
    """
    config_path = Path(config_path)
    text = read_config_text(config_path)
    if file_version(text) != version:
        raise ConfigError(
            "config.yaml changed since the page loaded it. Reload to see the "
            "new values, then make your changes again."
        )
    if not isinstance(changes, dict):
        raise ConfigError(
            'changes must be an object like {"pack.cells_per_module": 12}'
        )

    document = parse_config_text(text, config_path)
    for path, value in changes.items():
        set_setting(document, path, value)
    return document


def set_setting(document: dict, path: str, value) -> None:
    """Set one dotted path. It must be a setting in the file, or one of
    OPTIONAL_SETTINGS. An unchanged value is left alone, so its formatting
    stays exactly as written."""
    if isinstance(value, (dict, list)) or not isinstance(path, str):
        raise ConfigError(f"{path}: must be a single value")
    *sections, key = path.split(".")

    target = document
    for section in sections:
        if section not in target and path in OPTIONAL_SETTINGS:
            target[section] = {}
        target = target.get(section)
        if not isinstance(target, dict):
            raise ConfigError(f"{path}: not a setting in config.yaml")

    if key not in target:
        if path not in OPTIONAL_SETTINGS:
            raise ConfigError(f"{path}: not a setting in config.yaml")
        target[key] = value
        return

    old = target[key]
    if isinstance(old, dict):
        raise ConfigError(f"{path}: is a section, not a single value")
    if same_value(old, value):
        return
    # A hex number stays hex, like 0x200 for a CAN ID.
    if (
        isinstance(old, HexInt)
        and isinstance(value, int)
        and not isinstance(value, bool)
    ):
        value = HexInt(value)
    target[key] = value


def same_value(old, new) -> bool:
    # True == 1 in Python, so a bool only equals a bool.
    if isinstance(old, bool) != isinstance(new, bool):
        return False
    return old == new


def write_config_file(config_path: Path, document: dict) -> str:
    """Write the settings, keeping comments and key order. Returns the new
    version.

    Written to a temporary file in the same folder first, then moved over
    config.yaml with os.replace, which happens in one step: the file is always
    either the old or the new one, never half written.
    """
    config_path = Path(config_path)
    stream = io.StringIO()
    make_yaml().dump(document, stream)
    text = stream.getvalue()

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=config_path.parent,
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            delete=False,  # kept after closing, so it can be moved
        ) as temp:
            temp_path = Path(temp.name)
            temp.write(text)
            temp.flush()
            os.fsync(temp.fileno())  # on the disk before the rename
        # The temporary file is private (0600); keep config.yaml's permissions.
        os.chmod(temp_path, stat.S_IMODE(config_path.stat().st_mode))
        os.replace(temp_path, config_path)
    except BaseException:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)  # never leave it behind
        raise
    return file_version(text)
