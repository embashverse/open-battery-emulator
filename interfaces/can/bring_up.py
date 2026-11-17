"""Bring the CAN bus up, before the emulator starts (REQ-30).

Linux only lets root create or configure network devices, so this is a separate
step from the emulator, which runs as a normal user. On the Pi, systemd runs it
at boot (deploy/open-battery-emulator-can.service). By hand:

    sudo .venv/bin/python -m interfaces.can.bring_up --config config.yaml

It reads interfaces.can.channel and interfaces.can.bitrate from config.yaml:
- a channel starting with "vcan" (virtual CAN): load the vcan kernel module and
  create the device, then bring it up. A virtual bus has no bitrate.
- any other channel (a real adapter, like can0): set the bitrate, with
  restart-ms so the controller recovers from bus off by itself, then bring it up.

bring_up_commands() only builds the list of commands, so tests can check it
without root. main() runs them.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from interfaces.can.can_bus import CanSettingsError, whole_number

# --- 1. Constants -------------------------------------------------------------

# After bus off (too many errors on the wire, the controller switches itself
# off), the kernel restarts the controller after this many milliseconds.
RESTART_MS = 100

# A Linux network device name: letters, digits, "_", "." and "-", at most 15
# characters. Checking it gives a clear error instead of a confusing ip error.
DEVICE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,15}")


# --- 2. Reading config.yaml ---------------------------------------------------


def read_config(path: Path) -> dict:
    """Load config.yaml as nested dictionaries."""
    if not path.is_file():
        raise CanSettingsError(f"config file not found: {path}")
    try:
        raw = YAML(typ="safe").load(path.read_text())
    except YAMLError as error:
        raise CanSettingsError(f"{path} is not valid YAML: {error}") from None
    if not isinstance(raw, dict):
        raise CanSettingsError(f"{path} must contain settings as 'name: value' lines")
    return raw


# --- 3. Checking the settings -------------------------------------------------


def can_settings(raw: dict) -> tuple[str, int] | None:
    """(channel, bitrate) from interfaces.can, or None if there is nothing to
    bring up: no CAN section, CAN disabled, or python-can's virtual driver
    (an in-memory bus, no Linux device)."""
    interfaces = raw.get("interfaces") or {}
    settings = interfaces.get("can") if isinstance(interfaces, dict) else None
    if not isinstance(settings, dict):
        return None
    if settings.get("enabled") is not True:
        return None
    if settings.get("driver", "socketcan") == "virtual":
        return None

    channel = settings.get("channel")
    if channel is None:
        raise CanSettingsError("interfaces.can.channel is missing")
    if not isinstance(channel, str) or not DEVICE_NAME.fullmatch(channel):
        raise CanSettingsError(
            f"interfaces.can.channel '{channel}' is not a valid device name "
            "(letters, digits, '_', '.', '-', at most 15 characters, like can0)"
        )
    bitrate = whole_number(settings, "bitrate", "interfaces.can.bitrate", minimum=1)
    return channel, bitrate


# --- 4. Building the commands -------------------------------------------------


def bring_up_commands(
    channel: str, bitrate: int, device_exists: bool
) -> list[list[str]]:
    """The commands that bring the bus up, in order. Nothing is run here.

    device_exists: the device is already there (for vcan: created by an
    earlier run), so it is not created again.
    """
    if channel.startswith("vcan"):
        commands = [["modprobe", "vcan"]]
        if not device_exists:
            commands.append(["ip", "link", "add", "dev", channel, "type", "vcan"])
        commands.append(["ip", "link", "set", "up", channel])
        return commands

    # A real adapter. The bitrate can only be changed while the device is down,
    # so take it down first; that also makes running this twice safe.
    set_type = ["ip", "link", "set", channel, "type", "can"]
    set_timing = ["bitrate", str(bitrate), "restart-ms", str(RESTART_MS)]
    return [
        ["ip", "link", "set", channel, "down"],
        set_type + set_timing,
        ["ip", "link", "set", channel, "up"],
    ]


# --- 5. Running ---------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m interfaces.can.bring_up",
        description="Bring the CAN bus from config.yaml up (needs root).",
    )
    parser.add_argument(
        "--config", default="config.yaml", help="settings file (default config.yaml)"
    )
    args = parser.parse_args(argv)

    try:
        found = can_settings(read_config(Path(args.config)))
    except CanSettingsError as error:
        print(f"Config error: {error}", file=sys.stderr)
        return 1
    if found is None:
        print("CAN is disabled or uses the virtual driver: nothing to bring up.")
        return 0
    channel, bitrate = found

    if os.geteuid() != 0:
        print("Bringing up the CAN bus needs root: run it with sudo.", file=sys.stderr)
        return 1

    device_exists = Path("/sys/class/net", channel).exists()
    for command in bring_up_commands(channel, bitrate, device_exists):
        print("+", " ".join(command))
        result = subprocess.run(command, check=False)  # returncode checked below
        if result.returncode != 0:
            print(f"Failed: {' '.join(command)}", file=sys.stderr)
            return 1
    print(f"{channel} is up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
