"""Low level: one cell.

Reads the battery CSV files, looks up OCV, updates SOC by coulomb counting,
calculates cell voltage and finds the allowed charge and discharge current.

Units: SOC in percent (0 to 100), current in amps with discharge positive,
temperature in degrees C, voltage in volts.
"""

import csv
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

# Names of the two OCV curves a cell can be on.
CHARGE = "charge"
DISCHARGE = "discharge"


# --- 1. Data shapes -----------------------------------------------------------


@dataclass
class Table:
    """One battery table: SOC rows, temperature columns, a value in each cell."""

    file: Path
    socs: list[float]  # row headers, ascending
    temps: list[float]  # column headers, ascending
    values: list[list[float]]  # values[row][column]


@dataclass
class Battery:
    """Everything in one battery folder. Shared by all cells of a pack."""

    capacity_ah: float
    r0_ohm: float
    v_min: float
    v_max: float
    ocv_charge: Table
    ocv_discharge: Table
    limit_charge: Table
    limit_discharge: Table


@dataclass
class Cell:
    """What is different for every cell: its own capacity, R0 and SOC."""

    capacity_ah: float
    r0_ohm: float
    soc_pct: float
    ocv_curve: str = DISCHARGE  # the curve of the last current direction


@dataclass
class CurrentLimits:
    """Allowed current for one cell, both as positive amps."""

    charge_a: float
    discharge_a: float
    temp_out_of_range: bool


# --- 2. Loading a battery folder (once, at start) -----------------------------


def load_battery(folder: Path) -> Battery:
    """Read cell.csv and the four tables from one battery folder."""
    folder = Path(folder)
    cell_values = read_cell_file(folder / "cell.csv")
    return Battery(
        capacity_ah=cell_values["capacity"],
        r0_ohm=cell_values["r0"],
        v_min=cell_values["v_min"],
        v_max=cell_values["v_max"],
        ocv_charge=read_table(folder / "ocv_charge.csv"),
        ocv_discharge=read_table(folder / "ocv_discharge.csv"),
        limit_charge=read_table(folder / "limit_charge.csv"),
        limit_discharge=read_table(folder / "limit_discharge.csv"),
    )


def read_cell_file(path: Path) -> dict[str, float]:
    """Read cell.csv (columns: parameter, value, unit) into a dict of numbers."""
    values = {}
    with open(path, newline="") as file:
        for row in csv.DictReader(file):
            name = row["parameter"].strip()
            values[name] = to_number(row["value"], path)

    for name in ("capacity", "r0", "v_min", "v_max"):
        if name not in values:
            raise ValueError(f"{path}: missing parameter '{name}'")
    if values["capacity"] <= 0:
        raise ValueError(f"{path}: capacity must be greater than 0")
    if values["r0"] < 0:
        raise ValueError(f"{path}: r0 must not be negative")
    return values


def read_table(path: Path) -> Table:
    """Read one table: first row 'soc,temp1,temp2,...', then one row per SOC."""
    with open(path, newline="") as file:
        rows = [row for row in csv.reader(file) if row]  # skip blank lines

    if not rows or rows[0][0].strip().lower() != "soc":
        raise ValueError(f"{path}: first row must start with 'soc'")

    header = rows[0]
    temps = [to_number(text, path) for text in header[1:]]
    if not temps:
        raise ValueError(f"{path}: needs at least one temperature column")

    socs = []
    values = []
    for row in rows[1:]:
        if len(row) != len(header):
            raise ValueError(
                f"{path}: row '{','.join(row)}' has {len(row)} values, "
                f"the header has {len(header)}"
            )
        socs.append(to_number(row[0], path))
        values.append([to_number(text, path) for text in row[1:]])
    if not socs:
        raise ValueError(f"{path}: needs at least one SOC row")

    check_ascending(socs, "SOC rows", path)
    check_ascending(temps, "temperature columns", path)
    return Table(file=path, socs=socs, temps=temps, values=values)


def to_number(text: str, path: Path) -> float:
    """Turn a CSV field into a number, or fail with the file name."""
    try:
        return float(text)
    except ValueError:
        raise ValueError(f"{path}: '{text}' is not a number") from None


def check_ascending(numbers: list[float], what: str, path: Path) -> None:
    """Fail if the numbers are not strictly ascending (no duplicates)."""
    for before, after in pairwise(numbers):
        if after <= before:
            raise ValueError(
                f"{path}: {what} must be in ascending order with no duplicates "
                f"({before:g} is followed by {after:g})"
            )


# --- 3. Table lookup ----------------------------------------------------------


def find_position(points: list[float], x: float) -> tuple[int, int, float]:
    """Find where x sits between the points.

    Returns (left index, right index, fraction from left to right).
    Outside the points the nearest point is used, so left == right.
    """
    if x <= points[0]:
        return 0, 0, 0.0
    if x >= points[-1]:
        last = len(points) - 1
        return last, last, 0.0
    for i in range(len(points) - 1):
        left = points[i]
        right = points[i + 1]
        if x <= right:
            return i, i + 1, (x - left) / (right - left)
    raise AssertionError("unreachable: x is inside the points")


def interpolate(a: float, b: float, fraction: float) -> float:
    """Straight line from a (fraction 0) to b (fraction 1)."""
    return a + (b - a) * fraction


def lookup(table: Table, soc_pct: float, temp_c: float) -> float:
    """Value at this SOC and temperature, interpolated in both (bilinear).

    Step 1: in the two SOC rows around soc_pct, interpolate along temperature.
    Step 2: interpolate between those two results along SOC.
    """
    low_row, high_row, soc_fraction = find_position(table.socs, soc_pct)
    left_col, right_col, temp_fraction = find_position(table.temps, temp_c)

    low_values = table.values[low_row]
    high_values = table.values[high_row]
    at_low_soc = interpolate(low_values[left_col], low_values[right_col], temp_fraction)
    at_high_soc = interpolate(
        high_values[left_col], high_values[right_col], temp_fraction
    )
    return interpolate(at_low_soc, at_high_soc, soc_fraction)


# --- 4. Making a cell ---------------------------------------------------------


def new_cell(battery: Battery, soc_pct: float) -> Cell:
    """A cell with exactly the battery file values.

    pack.py can build Cell(...) directly to give each cell its own values.
    """
    return Cell(capacity_ah=battery.capacity_ah, r0_ohm=battery.r0_ohm, soc_pct=soc_pct)


# --- 5. Each tick: OCV curve and SOC ------------------------------------------


def update_cell(cell: Cell, current_a: float, dt_s: float) -> None:
    """Move the cell forward by dt_s seconds with this current."""
    # Pick the OCV curve by current direction. At 0 A keep the last one.
    if current_a > 0:
        cell.ocv_curve = DISCHARGE
    elif current_a < 0:
        cell.ocv_curve = CHARGE

    # Coulomb counting: amp hours taken out, as a percent of capacity.
    used_ah = current_a * dt_s / 3600
    soc = cell.soc_pct - used_ah / cell.capacity_ah * 100

    # SOC stays between 0 and 100 %.
    cell.soc_pct = max(0.0, min(100.0, soc))


# --- 6. Voltage ---------------------------------------------------------------


def open_circuit_voltage(cell: Cell, battery: Battery, temp_c: float) -> float:
    """OCV from the charge or discharge table, whichever the cell is on."""
    if cell.ocv_curve == CHARGE:
        table = battery.ocv_charge
    else:
        table = battery.ocv_discharge
    return lookup(table, cell.soc_pct, temp_c)


def cell_voltage(
    cell: Cell, battery: Battery, current_a: float, temp_c: float
) -> float:
    """Cell voltage = OCV - I x R0. Discharge (I > 0) pulls it down."""
    return open_circuit_voltage(cell, battery, temp_c) - current_a * cell.r0_ohm


# --- 7. Current limits --------------------------------------------------------


def temperature_in_range(table: Table, temp_c: float) -> bool:
    """First and last column are the operating range. One column: always in range."""
    if len(table.temps) == 1:
        return True
    return table.temps[0] <= temp_c <= table.temps[-1]


def current_limits(cell: Cell, battery: Battery, temp_c: float) -> CurrentLimits:
    """Allowed charge and discharge current at this cell's SOC and temperature."""
    in_range = temperature_in_range(
        battery.limit_charge, temp_c
    ) and temperature_in_range(battery.limit_discharge, temp_c)
    if not in_range:
        return CurrentLimits(charge_a=0.0, discharge_a=0.0, temp_out_of_range=True)

    return CurrentLimits(
        charge_a=lookup(battery.limit_charge, cell.soc_pct, temp_c),
        discharge_a=lookup(battery.limit_discharge, cell.soc_pct, temp_c),
        temp_out_of_range=False,
    )
