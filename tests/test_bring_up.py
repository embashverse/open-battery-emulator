"""Tests for interfaces/can/bring_up.py: reading channel and bitrate and building
the ip commands. Nothing is run, so these tests need no root and no CAN device.
"""

import re

import pytest

from interfaces.can.bring_up import bring_up_commands, can_settings
from interfaces.can.can_bus import CanSettingsError


def config_with_can(**settings):
    """A config.yaml as nested dictionaries, with only an interfaces.can section."""
    can = {
        "enabled": True,
        "driver": "socketcan",
        "channel": "vcan0",
        "bitrate": 500000,
    }
    can.update(settings)
    return {"interfaces": {"can": can}}


# --- Commands -----------------------------------------------------------------


def test_vcan0_loads_the_module_creates_and_brings_up_the_device():
    """Checks REQ-30: a vcan channel loads vcan, creates the device, brings it up."""
    channel, bitrate = can_settings(config_with_can(channel="vcan0"))

    commands = bring_up_commands(channel, bitrate, device_exists=False)

    assert commands == [
        ["modprobe", "vcan"],
        ["ip", "link", "add", "dev", "vcan0", "type", "vcan"],
        ["ip", "link", "set", "up", "vcan0"],
    ]


def test_existing_vcan0_is_not_created_again():
    """Checks REQ-30: running bring up a second time does not fail on 'add'."""
    commands = bring_up_commands("vcan0", 500000, device_exists=True)

    assert commands == [
        ["modprobe", "vcan"],
        ["ip", "link", "set", "up", "vcan0"],
    ]


def test_can0_sets_bitrate_and_restart_ms_then_brings_it_up():
    """Checks REQ-30: a real channel gets the bitrate from config.yaml and
    restart-ms 100, so it recovers from bus off, then it is brought up."""
    channel, bitrate = can_settings(config_with_can(channel="can0", bitrate=500000))

    commands = bring_up_commands(channel, bitrate, device_exists=True)

    # Joined with spaces, each command reads as it would be typed in a shell.
    assert [" ".join(command) for command in commands] == [
        "ip link set can0 down",
        "ip link set can0 type can bitrate 500000 restart-ms 100",
        "ip link set can0 up",
    ]


# --- Settings -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({"channel": None}, "interfaces.can.channel is missing"),
        ({"channel": "can 0"}, "interfaces.can.channel 'can 0' is not a valid"),
        ({"channel": 0}, "interfaces.can.channel '0' is not a valid"),
        ({"channel": "a_very_long_name0"}, "interfaces.can.channel 'a_very_long"),
        ({"bitrate": None}, "interfaces.can.bitrate is missing"),
        ({"bitrate": "fast"}, "interfaces.can.bitrate must be a whole number"),
        ({"bitrate": 0}, "interfaces.can.bitrate must be at least 1"),
    ],
)
def test_bad_channel_or_bitrate_is_a_clear_error(settings, message):
    """Checks REQ-30: a missing or wrong channel or bitrate names the setting."""
    raw = config_with_can(**settings)
    for key, value in settings.items():
        if value is None:
            del raw["interfaces"]["can"][key]

    with pytest.raises(CanSettingsError, match=re.escape(message)):
        can_settings(raw)


@pytest.mark.parametrize(
    "raw",
    [
        config_with_can(enabled=False),
        config_with_can(driver="virtual"),
        {"interfaces": {}},
        {},
    ],
)
def test_nothing_to_bring_up_without_a_socketcan_device(raw):
    """Checks REQ-30: CAN disabled, virtual driver or no CAN section: nothing to do."""
    assert can_settings(raw) is None
