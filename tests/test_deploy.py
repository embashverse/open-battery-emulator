"""Tests for deploy/install.sh and the two systemd unit files.

install.sh --dry-run prints the unit files with the user and repo path filled
in, without needing root and without touching systemd. These tests read that
output. Whether systemd really starts, orders and restarts the services is
checked by hand on the Pi (Manual checks in
docs/engineering/verification.md).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent.resolve()
INSTALL = REPO / "deploy" / "install.sh"
BOOTSTRAP = REPO / "deploy" / "bootstrap.sh"
CAN_UNIT = "open-battery-emulator-can.service"
EMULATOR_UNIT = "open-battery-emulator.service"
TEST_USER = "pi_user"  # stands in for the user who calls sudo

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")


# --- Helpers ------------------------------------------------------------------


def dry_run_units() -> dict[str, dict[str, list[str]]]:
    """Run install.sh --dry-run and return each printed unit file as
    {"Setting": [values]}. A list, because a setting may appear more than once.

    SUDO_USER is set as if a normal user had called sudo, so the filled in
    user is the same on every machine, also when the tests run as root.
    """
    env = {**os.environ, "SUDO_USER": TEST_USER}
    output = subprocess.run(
        ["bash", str(INSTALL), "--dry-run"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    ).stdout

    # The output looks like:
    #   would write /etc/systemd/system/<unit>:
    #       <unit file line, indented by 4 spaces>
    units = {}
    current = None
    for line in output.splitlines():
        if line.startswith("would write "):
            current = Path(line.removeprefix("would write ").rstrip(":")).name
            units[current] = {}
        elif current and line.startswith("    "):
            text = line.strip()
            if "=" in text and not text.startswith("#"):
                key, value = text.split("=", 1)
                units[current].setdefault(key, []).append(value)
        else:
            current = None  # "would run: ..." lines end a unit file
    return units


# --- Tests --------------------------------------------------------------------


def test_emulator_unit_runs_as_normal_user_after_the_bus_and_restarts():
    """Checks REQ-31, REQ-32: the emulator unit runs as the sudo user (not root),
    wants and waits for the bus unit without needing it to succeed (Wants, not
    Requires), restarts on failure, uses the .venv python."""
    unit = dry_run_units()[EMULATOR_UNIT]

    assert unit["User"] == [TEST_USER]
    assert unit["User"] != ["root"]
    assert unit["Wants"] == [CAN_UNIT]
    assert "Requires" not in unit  # a failed bus step must not block it
    assert unit["After"] == [CAN_UNIT]
    assert unit["Restart"] == ["on-failure"]

    command = unit["ExecStart"][0].split()
    assert command[0] == str(REPO / ".venv" / "bin" / "python")
    assert command[1:3] == ["-m", "emulator.engine"]
    assert command[command.index("--config") + 1] == str(REPO / "config.yaml")
    assert "@" not in unit["ExecStart"][0]  # every placeholder filled in


def test_bus_unit_runs_bring_up_as_root_with_the_repo_config():
    """Checks REQ-30, REQ-32: the bus unit runs bring_up as root (no User=)
    with the repo's config.yaml, once at boot."""
    unit = dry_run_units()[CAN_UNIT]

    assert "User" not in unit  # systemd runs a service as root by default
    assert unit["Type"] == ["oneshot"]
    assert unit["WantedBy"] == ["multi-user.target"]

    command = unit["ExecStart"][0].split()
    assert command[0] == str(REPO / ".venv" / "bin" / "python")
    assert command[1:3] == ["-m", "interfaces.can.bring_up"]
    assert command[command.index("--config") + 1] == str(REPO / "config.yaml")


def test_bootstrap_script_is_valid_bash():
    """Checks REQ-43: deploy/bootstrap.sh passes bash -n (syntax only, nothing
    is run) and is executable."""
    result = subprocess.run(
        ["bash", "-n", str(BOOTSTRAP)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr
    assert os.access(BOOTSTRAP, os.X_OK)
