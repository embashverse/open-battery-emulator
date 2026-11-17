"""Shared test helpers: a small test battery, a small config.yaml and a fake clock.

Used by test_engine.py and test_can.py (imported with `from conftest import ...`).
Everything is written in tmp_path, so tests do not depend on batteries/ or the
real config.yaml.
"""

from ruamel.yaml import YAML

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
    """One cell, SOC 50, 25 degC, no variation, 10 A simulated, no interfaces."""
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
    """Write battery and config.yaml in tmp_path, return the config path.

    changes: {"pack.cells_per_module": 0} or nested deeper, like
    {"interfaces.can": {...}} or {"interfaces.can.enabled": False}. Missing
    sections on the way are created. MISSING as a value removes the setting.
    """
    write_battery(tmp_path)
    config = base_config()
    for name, value in (changes or {}).items():
        *sections, key = name.split(".")
        target = config
        for section in sections:
            target = target.setdefault(section, {})
        if value is MISSING:
            del target[key]
        else:
            target[key] = value
    path = tmp_path / "config.yaml"
    with path.open("w") as file:
        YAML().dump(config, file)
    return path


# --- Fake clock ---------------------------------------------------------------


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
