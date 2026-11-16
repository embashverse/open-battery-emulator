"""Tests for emulator/pack.py: topology, current split, state machine, test controls.

All battery data is written here, so these tests do not depend on batteries/.
"""

import pytest

from emulator.cell import Cell
from emulator.pack import (
    CHARGING,
    DISCHARGING,
    EMPTY,
    FULL,
    IDLE,
    PackSettings,
    build_pack,
    cells_of_pack,
    set_hold_soc,
    set_soc,
    tick,
)

# --- Small test battery -------------------------------------------------------
# OCV is a straight line: 3.0 V at SOC 0 to 3.5 V at SOC 100, so 5 mV per %.
# The charge curve is 0.1 V higher. Both columns are the same, so temperature
# only matters for the operating range (0 to 40 degC).
#
# Handy values (discharge curve):  SOC 10 -> 3.05 V (v_min), 50 -> 3.25 V
# Handy values (charge curve):     SOC 70 -> 3.45 V (v_max), 50 -> 3.35 V

CELL_CSV = """parameter,value,unit
capacity,100,Ah
r0,0.001,ohm
v_min,3.05,V
v_max,3.45,V
"""

OCV_DISCHARGE = """soc,0,40
0,3.0,3.0
100,3.5,3.5
"""

OCV_CHARGE = """soc,0,40
0,3.1,3.1
100,3.6,3.6
"""

# Charge limit 100 A at SOC 0 down to 0 A at SOC 100; discharge the other way.
LIMIT_CHARGE = """soc,0,40
0,100,100
100,0,0
"""

LIMIT_DISCHARGE = """soc,0,40
0,0,0
100,100,100
"""


def write_battery(folder, cell=CELL_CSV):
    """Write a battery folder; cell.csv can be swapped for a test."""
    (folder / "cell.csv").write_text(cell)
    (folder / "ocv_charge.csv").write_text(OCV_CHARGE)
    (folder / "ocv_discharge.csv").write_text(OCV_DISCHARGE)
    (folder / "limit_charge.csv").write_text(LIMIT_CHARGE)
    (folder / "limit_discharge.csv").write_text(LIMIT_DISCHARGE)
    return folder


def settings(**changes):
    """One cell, SOC 50, 25 degC, no variation. Any value can be changed."""
    values = {
        "cells_per_module": 1,
        "modules_in_series": 1,
        "strings_in_parallel": 1,
        "start_soc_pct": 50,
        "temperature_c": 25,
        "capacity_variation_pct": 0,
        "r0_variation_pct": 0,
        "soc_variation_pct": 0,
        "seed": 1,
        "deadband_a": 0.5,
    }
    values.update(changes)
    return PackSettings(**values)


@pytest.fixture
def folder(tmp_path):
    return write_battery(tmp_path)


def make_pack(folder, **changes):
    return build_pack(folder, settings(**changes))


def first_cell(pack, string=0):
    return pack.strings[string].modules[0].cells[0]


# --- State table: one test per row --------------------------------------------


@pytest.mark.parametrize("from_state", [IDLE, CHARGING])
def test_discharge_current_goes_to_discharging(folder, from_state):
    """Checks REQ-07: IDLE, CHARGING with I > 0 -> DISCHARGING."""
    pack = make_pack(folder)
    pack.state = from_state
    assert tick(pack, current_a=10, dt_s=0.1).state == DISCHARGING


@pytest.mark.parametrize("from_state", [IDLE, DISCHARGING])
def test_charge_current_goes_to_charging(folder, from_state):
    """Checks REQ-07: IDLE, DISCHARGING with I < 0 -> CHARGING."""
    pack = make_pack(folder)
    pack.state = from_state
    assert tick(pack, current_a=-10, dt_s=0.1).state == CHARGING


@pytest.mark.parametrize("from_state", [CHARGING, DISCHARGING])
def test_zero_current_goes_to_idle(folder, from_state):
    """Checks REQ-07: CHARGING, DISCHARGING with I = 0 -> IDLE."""
    pack = make_pack(folder)
    pack.state = from_state
    assert tick(pack, current_a=0, dt_s=0.1).state == IDLE


def test_lowest_cell_at_v_min_goes_to_empty(folder):
    """Checks REQ-07: DISCHARGING with lowest cell <= v_min -> EMPTY."""
    # SOC 11: OCV 3.055 V, at 10 A the cell shows 3.045 V, below v_min 3.05 V.
    pack = make_pack(folder, start_soc_pct=11)
    pack.state = DISCHARGING
    result = tick(pack, current_a=10, dt_s=0.1)
    assert result.lowest_cell.voltage_v <= 3.05
    assert result.state == EMPTY


def test_highest_cell_at_v_max_goes_to_full(folder):
    """Checks REQ-07: CHARGING with highest cell >= v_max -> FULL."""
    # SOC 69: charge OCV 3.445 V, at -10 A the cell shows 3.455 V, above 3.45 V.
    pack = make_pack(folder, start_soc_pct=69)
    pack.state = CHARGING
    result = tick(pack, current_a=-10, dt_s=0.1)
    assert result.highest_cell.voltage_v >= 3.45
    assert result.state == FULL


def test_charge_current_leaves_empty(folder):
    """Checks REQ-07: EMPTY with I < 0 -> CHARGING."""
    pack = make_pack(folder, start_soc_pct=11)
    pack.state = EMPTY
    assert tick(pack, current_a=-10, dt_s=0.1).state == CHARGING


def test_discharge_current_leaves_full(folder):
    """Checks REQ-07: FULL with I > 0 -> DISCHARGING."""
    pack = make_pack(folder, start_soc_pct=69)
    pack.state = FULL
    assert tick(pack, current_a=10, dt_s=0.1).state == DISCHARGING


# --- Blocking -----------------------------------------------------------------


def test_full_blocks_charging(folder):
    """Checks REQ-07: FULL blocks charging and stays FULL."""
    pack = make_pack(folder, start_soc_pct=69)
    pack.state = FULL
    for current_a in (-50, 0, -50):
        result = tick(pack, current_a=current_a, dt_s=60)
        assert result.state == FULL
        assert result.current_a == 0
        assert result.soc_pct == pytest.approx(69)


def test_empty_blocks_discharging(folder):
    """Checks REQ-07: EMPTY blocks discharging and stays EMPTY."""
    pack = make_pack(folder, start_soc_pct=11)
    pack.state = EMPTY
    for current_a in (50, 0, 50):
        result = tick(pack, current_a=current_a, dt_s=60)
        assert result.state == EMPTY
        assert result.current_a == 0
        assert result.soc_pct == pytest.approx(11)


# --- Deadband -----------------------------------------------------------------


def test_noise_inside_deadband_changes_nothing(folder):
    """Checks REQ-24: currents within +-deadband_a count as 0."""
    pack = make_pack(folder, deadband_a=0.5)
    for noise_a in (0.3, -0.3, 0.5, -0.5):
        result = tick(pack, current_a=noise_a, dt_s=3600)
        assert result.state == IDLE
        assert result.current_a == 0
        assert result.soc_pct == 50
        assert result.lowest_cell.voltage_v == pytest.approx(3.25)  # no I x R0

    # Just outside the deadband the current flows.
    assert tick(pack, current_a=0.6, dt_s=0.1).state == DISCHARGING


# --- Cell variation -----------------------------------------------------------


def test_weakest_cell_reaches_v_min_first(folder):
    """Checks REQ-07 and REQ-20: the cell with the least capacity sets EMPTY."""
    pack = make_pack(folder, cells_per_module=3, start_soc_pct=40)
    weak = pack.strings[0].modules[0].cells[1]
    weak.capacity_ah = 50  # empties twice as fast as the others

    for _ in range(10_000):
        result = tick(pack, current_a=50, dt_s=1)
        if result.state == EMPTY:
            break

    assert result.state == EMPTY
    assert (result.lowest_cell.module, result.lowest_cell.cell) == (1, 2)
    others = [c for c in cells_of_pack(pack) if c is not weak]
    assert all(c.soc_pct > weak.soc_pct + 5 for c in others)


def test_same_seed_gives_identical_packs(folder):
    """Checks REQ-20."""
    varied = {
        "cells_per_module": 4,
        "modules_in_series": 2,
        "strings_in_parallel": 2,
        "capacity_variation_pct": 2,
        "r0_variation_pct": 5,
        "soc_variation_pct": 1,
        "seed": 42,
    }
    first = cells_of_pack(make_pack(folder, **varied))
    second = cells_of_pack(make_pack(folder, **varied))
    assert first == second
    # The cells really are different from each other.
    assert len({c.capacity_ah for c in first}) == len(first)
    # Another seed gives another pack.
    assert cells_of_pack(make_pack(folder, **{**varied, "seed": 43})) != first


def test_zero_variation_gives_identical_cells(folder):
    """Checks REQ-20."""
    pack = make_pack(
        folder, cells_per_module=4, modules_in_series=2, strings_in_parallel=2
    )
    expected = Cell(capacity_ah=100, r0_ohm=0.001, soc_pct=50)
    assert all(cell == expected for cell in cells_of_pack(pack))
    assert len(cells_of_pack(pack)) == 16


# --- Current split ------------------------------------------------------------


def test_string_currents_add_up_to_pack_current(folder):
    """Checks REQ-05."""
    pack = make_pack(
        folder,
        cells_per_module=4,
        modules_in_series=2,
        strings_in_parallel=3,
        capacity_variation_pct=2,
        r0_variation_pct=5,
        soc_variation_pct=3,
    )
    for current_a in (-80, -3, 0, 2, 50, 120):
        result = tick(pack, current_a=current_a, dt_s=1)
        total = sum(s.current_a for s in result.strings)
        assert total == pytest.approx(current_a, abs=1e-9)


def test_identical_strings_share_current_equally(folder):
    """Checks REQ-05."""
    pack = make_pack(folder, cells_per_module=3, strings_in_parallel=2)
    result = tick(pack, current_a=100, dt_s=1)
    assert [s.current_a for s in result.strings] == pytest.approx([50, 50])


def test_higher_resistance_string_takes_less_current(folder):
    """Checks REQ-05."""
    pack = make_pack(folder, strings_in_parallel=2)
    first_cell(pack, string=1).r0_ohm = 0.002  # twice the resistance
    result = tick(pack, current_a=90, dt_s=0.1)
    # Same OCV, so current splits 2 : 1.
    assert [s.current_a for s in result.strings] == pytest.approx([60, 30])
    # Both strings show the same voltage: OCV 3.25 - 60 A x 1 mOhm.
    assert [s.voltage_v for s in result.strings] == pytest.approx(
        [3.19, 3.19], abs=1e-3
    )


def test_strings_at_different_soc_exchange_circulating_current(folder):
    """Checks REQ-05: at 0 A the fuller string charges the emptier one."""
    pack = make_pack(folder, strings_in_parallel=2)
    first_cell(pack, string=0).soc_pct = 60
    first_cell(pack, string=1).soc_pct = 40

    # First tick: OCV 3.30 V and 3.20 V, 2 mOhm in the loop: 50 A.
    result = tick(pack, current_a=0, dt_s=60)
    assert result.current_a == 0
    assert result.state == IDLE
    assert result.strings[0].current_a == pytest.approx(50)
    assert result.strings[1].current_a == pytest.approx(-50)

    gap = result.strings[0].soc_pct - result.strings[1].soc_pct
    for _ in range(50):
        result = tick(pack, current_a=0, dt_s=60)
        new_gap = result.strings[0].soc_pct - result.strings[1].soc_pct
        assert result.strings[0].current_a > 0  # never flips direction
        assert 0 < new_gap < gap
        gap = new_gap
    assert gap < 1


def test_zero_resistance_falls_back_to_equal_split(tmp_path):
    """Checks REQ-05: R0 of 0 gives an equal split."""
    no_r0 = CELL_CSV.replace("r0,0.001,ohm", "r0,0,ohm")
    pack = build_pack(
        write_battery(tmp_path, cell=no_r0), settings(strings_in_parallel=2)
    )
    first_cell(pack, string=0).soc_pct = 70
    first_cell(pack, string=1).soc_pct = 30

    result = tick(pack, current_a=100, dt_s=0.1)
    assert [s.current_a for s in result.strings] == [50, 50]
    result = tick(pack, current_a=0, dt_s=0.1)
    assert [s.current_a for s in result.strings] == [0, 0]


# --- Results ------------------------------------------------------------------


def test_module_and_cell_numbering(folder):
    """Checks REQ-03: modules numbered from 1 string by string, cells from 1."""
    pack = make_pack(
        folder, cells_per_module=3, modules_in_series=2, strings_in_parallel=2
    )
    pack.strings[1].modules[0].cells[1].soc_pct = 60  # module 3, cell 2
    pack.strings[0].modules[1].cells[2].soc_pct = 40  # module 2, cell 3

    result = tick(pack, current_a=0, dt_s=0.1)

    assert (result.highest_cell.module, result.highest_cell.cell) == (3, 2)
    assert (result.lowest_cell.module, result.lowest_cell.cell) == (2, 3)
    assert len(result.module_cell_voltages) == 4
    assert all(len(voltages) == 3 for voltages in result.module_cell_voltages)
    assert result.module_cell_voltages[2][1] == result.highest_cell.voltage_v
    assert result.module_cell_voltages[1][2] == result.lowest_cell.voltage_v


def test_pack_limits_are_p_times_lowest_cell_limit(folder):
    """Checks REQ-06 and REQ-22."""
    pack = make_pack(folder, cells_per_module=2, strings_in_parallel=2)
    pack.strings[1].modules[0].cells[0].soc_pct = 80
    set_hold_soc(pack, True)  # keep SOC exact despite the circulating current

    result = tick(pack, current_a=0, dt_s=0.1)
    # Charge limit: 50 A at SOC 50, 20 A at SOC 80. Lowest is 20 A, x 2 strings.
    assert result.max_charge_a == pytest.approx(40)
    # Discharge limit: 50 A at SOC 50, 80 A at SOC 80. Lowest is 50 A, x 2.
    assert result.max_discharge_a == pytest.approx(100)
    assert result.temp_out_of_range is False

    pack.temperature_c = 50  # outside the 0 to 40 degC range
    result = tick(pack, current_a=0, dt_s=0.1)
    assert result.max_charge_a == 0
    assert result.max_discharge_a == 0
    assert result.temp_out_of_range is True


# --- Test controls ------------------------------------------------------------


def test_set_soc_keeps_the_spread(folder):
    """Checks REQ-21: each cell keeps its offset from the average, clamped."""
    pack = make_pack(
        folder, cells_per_module=6, strings_in_parallel=2, soc_variation_pct=2
    )
    before = [c.soc_pct for c in cells_of_pack(pack)]
    average_before = sum(before) / len(before)

    set_soc(pack, 90)
    after = [c.soc_pct for c in cells_of_pack(pack)]
    assert sum(after) / len(after) == pytest.approx(90)
    for old, new in zip(before, after, strict=True):
        assert new - 90 == pytest.approx(old - average_before)

    set_soc(pack, 99.5)
    after = [c.soc_pct for c in cells_of_pack(pack)]
    assert max(after) == 100
    assert min(after) < 99.5


def test_hold_soc_freezes_soc_but_voltage_shows_drop(folder):
    """Checks REQ-21: SOC stays, voltage still shows I x R0."""
    pack = make_pack(folder)
    set_hold_soc(pack, True)

    result = tick(pack, current_a=100, dt_s=3600)
    assert result.soc_pct == 50
    assert result.state == DISCHARGING
    assert result.voltage_v == pytest.approx(3.25 - 100 * 0.001)

    set_hold_soc(pack, False)
    result = tick(pack, current_a=100, dt_s=360)
    assert result.soc_pct == pytest.approx(40)
