"""Tests for emulator/cell.py: CSV loading, lookup, SOC, voltage, limits.

All battery data is written here, so these tests do not depend on batteries/.
"""

import pytest

from emulator.cell import (
    CHARGE,
    DISCHARGE,
    Cell,
    cell_voltage,
    current_limits,
    load_battery,
    lookup,
    new_cell,
    open_circuit_voltage,
    update_cell,
)

# --- Small test battery -------------------------------------------------------
# Two SOC rows (0 and 100) and two temperatures (0 and 40), so every expected
# value below can be worked out by hand with straight lines.

CELL_CSV = """parameter,value,unit
capacity,100,Ah
r0,0.001,ohm
v_min,2.5,V
v_max,3.65,V
"""

OCV_DISCHARGE = """soc,0,40
0,3.0,3.2
100,3.4,3.6
"""

# 0.1 V above the discharge curve, so we can tell which one was used.
OCV_CHARGE = """soc,0,40
0,3.1,3.3
100,3.5,3.7
"""

LIMIT_CHARGE = """soc,0,40
0,50,100
100,0,0
"""

LIMIT_DISCHARGE = """soc,0,40
0,0,0
100,50,100
"""


def write_battery(
    folder,
    cell=CELL_CSV,
    ocv_charge=OCV_CHARGE,
    ocv_discharge=OCV_DISCHARGE,
    limit_charge=LIMIT_CHARGE,
    limit_discharge=LIMIT_DISCHARGE,
):
    """Write a battery folder; any file can be swapped for a test."""
    (folder / "cell.csv").write_text(cell)
    (folder / "ocv_charge.csv").write_text(ocv_charge)
    (folder / "ocv_discharge.csv").write_text(ocv_discharge)
    (folder / "limit_charge.csv").write_text(limit_charge)
    (folder / "limit_discharge.csv").write_text(limit_discharge)
    return folder


@pytest.fixture
def battery(tmp_path):
    return load_battery(write_battery(tmp_path))


# --- Loading ------------------------------------------------------------------


def test_load_reads_cell_file_and_tables(battery):
    """Checks REQ-02."""
    assert battery.capacity_ah == 100
    assert battery.r0_ohm == 0.001
    assert battery.v_min == 2.5
    assert battery.v_max == 3.65
    assert battery.ocv_discharge.socs == [0, 100]
    assert battery.ocv_discharge.temps == [0, 40]
    assert battery.ocv_charge.values == [[3.1, 3.3], [3.5, 3.7]]
    assert battery.limit_charge.file.name == "limit_charge.csv"


def test_minimal_table_with_few_rows(tmp_path):
    """Checks REQ-02 and REQ-23: the minimal table from PROJECT.md is valid."""
    minimal = """soc,0,25,45
0,2.80,2.80,2.81
10,3.19,3.20,3.20
90,3.31,3.32,3.32
100,3.44,3.45,3.45
"""
    battery = load_battery(write_battery(tmp_path, ocv_discharge=minimal))
    # Halfway between the 10 and 90 rows at 25 degC: (3.20 + 3.32) / 2
    assert lookup(battery.ocv_discharge, 50, 25) == pytest.approx(3.26)


def test_rows_out_of_order_are_rejected_with_file_name(tmp_path):
    """Checks REQ-23."""
    bad = """soc,0,40
50,3.2,3.4
10,3.1,3.3
"""
    write_battery(tmp_path, ocv_discharge=bad)
    with pytest.raises(ValueError, match="ocv_discharge.csv"):
        load_battery(tmp_path)


def test_columns_out_of_order_are_rejected_with_file_name(tmp_path):
    """Checks REQ-23."""
    bad = """soc,40,0
0,0,0
100,50,100
"""
    write_battery(tmp_path, limit_discharge=bad)
    with pytest.raises(ValueError, match="limit_discharge.csv"):
        load_battery(tmp_path)


def test_duplicate_rows_are_rejected_with_file_name(tmp_path):
    """Checks REQ-23."""
    bad = """soc,0,40
0,50,100
0,50,100
100,0,0
"""
    write_battery(tmp_path, limit_charge=bad)
    with pytest.raises(ValueError, match="limit_charge.csv"):
        load_battery(tmp_path)


# --- Table lookup -------------------------------------------------------------


def test_interpolation_between_rows(battery):
    """Checks REQ-23."""
    # At 0 degC: 3.0 at SOC 0, 3.4 at SOC 100
    assert lookup(battery.ocv_discharge, 25, 0) == pytest.approx(3.1)
    assert lookup(battery.ocv_discharge, 50, 0) == pytest.approx(3.2)


def test_interpolation_between_columns(battery):
    """Checks REQ-23."""
    # At SOC 0: 3.0 at 0 degC, 3.2 at 40 degC
    assert lookup(battery.ocv_discharge, 0, 10) == pytest.approx(3.05)
    assert lookup(battery.ocv_discharge, 0, 20) == pytest.approx(3.1)


def test_interpolation_between_rows_and_columns(battery):
    """Checks REQ-23."""
    # At 10 degC: 3.05 at SOC 0, 3.45 at SOC 100, so 3.15 at SOC 25
    assert lookup(battery.ocv_discharge, 25, 10) == pytest.approx(3.15)


def test_nearest_row_outside_soc_range(tmp_path):
    """Checks REQ-23."""
    rows_10_to_90 = """soc,0,40
10,3.1,3.3
90,3.3,3.5
"""
    battery = load_battery(write_battery(tmp_path, ocv_discharge=rows_10_to_90))
    assert lookup(battery.ocv_discharge, 0, 0) == pytest.approx(3.1)
    assert lookup(battery.ocv_discharge, 5, 0) == pytest.approx(3.1)
    assert lookup(battery.ocv_discharge, 95, 0) == pytest.approx(3.3)
    assert lookup(battery.ocv_discharge, 100, 0) == pytest.approx(3.3)


def test_one_temperature_column_ignores_temperature(tmp_path):
    """Checks REQ-23 and REQ-22: one column means no temperature range."""
    ocv = """soc,25
0,3.0
100,3.4
"""
    limits = """soc,25
0,40
100,40
"""
    battery = load_battery(
        write_battery(
            tmp_path,
            ocv_charge=ocv,
            ocv_discharge=ocv,
            limit_charge=limits,
            limit_discharge=limits,
        )
    )
    cell = new_cell(battery, soc_pct=50)
    for temp_c in (-30, 25, 60):
        assert open_circuit_voltage(cell, battery, temp_c) == pytest.approx(3.2)
        result = current_limits(cell, battery, temp_c)
        assert result.charge_a == pytest.approx(40)
        assert result.discharge_a == pytest.approx(40)
        assert result.temp_out_of_range is False


def test_ocv_uses_nearest_column_outside_temperature_range(battery):
    """Checks REQ-23."""
    cell = new_cell(battery, soc_pct=0)
    assert open_circuit_voltage(cell, battery, -20) == pytest.approx(3.0)
    assert open_circuit_voltage(cell, battery, 60) == pytest.approx(3.2)


# --- SOC ----------------------------------------------------------------------


def test_100a_for_1h_empties_a_100ah_cell(battery):
    """Checks REQ-04: 100 A for 1 h on a 100 Ah cell takes SOC from 100 % to 0 %."""
    cell = new_cell(battery, soc_pct=100)
    for tick in range(36_000):  # 1 h of 100 ms ticks
        update_cell(cell, current_a=100, dt_s=0.1)
        if tick == 17_999:  # after 30 min
            assert cell.soc_pct == pytest.approx(50)
    assert cell.soc_pct == pytest.approx(0, abs=1e-6)


def test_charge_current_raises_soc(battery):
    """Checks REQ-04: charge is negative current."""
    cell = new_cell(battery, soc_pct=20)
    update_cell(cell, current_a=-50, dt_s=3600)
    assert cell.soc_pct == pytest.approx(70)


def test_soc_stays_between_0_and_100(battery):
    """Checks REQ-04."""
    cell = new_cell(battery, soc_pct=1)
    update_cell(cell, current_a=100, dt_s=3600)
    assert cell.soc_pct == 0

    cell = new_cell(battery, soc_pct=99)
    update_cell(cell, current_a=-100, dt_s=3600)
    assert cell.soc_pct == 100


def test_each_cell_uses_its_own_capacity(battery):
    """Checks REQ-20: a cell keeps its own capacity, R0 and SOC."""
    big = Cell(capacity_ah=100, r0_ohm=0.001, soc_pct=100)
    small = Cell(capacity_ah=50, r0_ohm=0.002, soc_pct=90)
    for cell in (big, small):
        update_cell(cell, current_a=25, dt_s=3600)
    assert big.soc_pct == pytest.approx(75)
    assert small.soc_pct == pytest.approx(40)
    # Different R0 gives a different drop at the same current.
    big.soc_pct = small.soc_pct = 50
    drop_big = 3.2 - cell_voltage(big, battery, current_a=100, temp_c=0)
    drop_small = 3.2 - cell_voltage(small, battery, current_a=100, temp_c=0)
    assert drop_big == pytest.approx(0.1)
    assert drop_small == pytest.approx(0.2)


# --- OCV curve and voltage ----------------------------------------------------


def test_ocv_curve_follows_current_direction_and_stays_at_idle(battery):
    """Checks REQ-05: discharge curve at start, the last one kept at 0 A."""
    cell = new_cell(battery, soc_pct=50)
    assert cell.ocv_curve == DISCHARGE
    assert open_circuit_voltage(cell, battery, 0) == pytest.approx(3.2)

    update_cell(cell, current_a=-10, dt_s=0.1)
    assert cell.ocv_curve == CHARGE

    update_cell(cell, current_a=0, dt_s=0.1)
    assert cell.ocv_curve == CHARGE
    assert open_circuit_voltage(cell, battery, 0) == pytest.approx(3.3, abs=1e-4)

    update_cell(cell, current_a=10, dt_s=0.1)
    assert cell.ocv_curve == DISCHARGE

    update_cell(cell, current_a=0, dt_s=0.1)
    assert cell.ocv_curve == DISCHARGE


def test_direction_current_picks_the_curve_when_given(battery):
    """Checks REQ-05: the pack current direction picks the curve, not the cell's."""
    cell = new_cell(battery, soc_pct=50)
    # The cell is charged by a circulating current while the pack is at 0 A.
    update_cell(cell, current_a=-10, dt_s=0.1, direction_current_a=0)
    assert cell.ocv_curve == DISCHARGE
    assert cell.soc_pct > 50  # SOC still follows the cell's own current

    # The pack discharges, but this cell is charged: still the discharge curve.
    update_cell(cell, current_a=-10, dt_s=0.1, direction_current_a=20)
    assert cell.ocv_curve == DISCHARGE

    update_cell(cell, current_a=10, dt_s=0.1, direction_current_a=-20)
    assert cell.ocv_curve == CHARGE


def test_cell_voltage_is_ocv_minus_i_times_r0(battery):
    """Checks REQ-05."""
    cell = new_cell(battery, soc_pct=50)  # discharge curve, OCV 3.2 V at 0 degC
    assert cell_voltage(cell, battery, current_a=100, temp_c=0) == pytest.approx(3.1)
    assert cell_voltage(cell, battery, current_a=0, temp_c=0) == pytest.approx(3.2)

    cell.ocv_curve = CHARGE  # charge curve, OCV 3.3 V at 0 degC
    assert cell_voltage(cell, battery, current_a=-100, temp_c=0) == pytest.approx(3.4)


# --- Current limits -----------------------------------------------------------


def test_limits_are_interpolated_from_limit_tables(battery):
    """Checks REQ-06."""
    cell = new_cell(battery, soc_pct=25)
    result = current_limits(cell, battery, temp_c=20)
    # At 20 degC: charge 75 A at SOC 0, 0 A at SOC 100
    assert result.charge_a == pytest.approx(56.25)
    # At 20 degC: discharge 0 A at SOC 0, 75 A at SOC 100
    assert result.discharge_a == pytest.approx(18.75)
    assert result.temp_out_of_range is False


def test_limits_are_zero_outside_temperature_range(battery):
    """Checks REQ-22."""
    cell = new_cell(battery, soc_pct=50)
    for temp_c in (-1, 41):
        result = current_limits(cell, battery, temp_c)
        assert result.charge_a == 0
        assert result.discharge_a == 0
        assert result.temp_out_of_range is True


def test_first_and_last_column_are_inside_temperature_range(battery):
    """Checks REQ-22."""
    cell = new_cell(battery, soc_pct=50)
    at_first = current_limits(cell, battery, temp_c=0)
    assert at_first.charge_a == pytest.approx(25)
    assert at_first.temp_out_of_range is False
    at_last = current_limits(cell, battery, temp_c=40)
    assert at_last.discharge_a == pytest.approx(50)
    assert at_last.temp_out_of_range is False
