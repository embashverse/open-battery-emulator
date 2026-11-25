"""Mid level: many cells.

Builds strings and modules, splits current across strings, finds the min and
max cell, works out pack values and runs the state machine. Uses only cell.py.

The pack is P strings in parallel. Each string is S modules in series, each
module C cells in series. Modules are numbered from 1, string by string: with
S = 8, string 1 has modules 1 to 8 and string 2 has modules 9 to 16. Cells are
numbered from 1 inside their module.

Units: SOC in percent (0 to 100), current in amps with discharge positive,
temperature in degrees C, voltage in volts.
"""

import random
from dataclasses import dataclass
from pathlib import Path

from emulator.cell import (
    Battery,
    Cell,
    cell_voltage,
    current_limits,
    load_battery,
    lookup,
    open_circuit_voltage,
    update_cell,
)

# --- 1. States ----------------------------------------------------------------

IDLE = "IDLE"
CHARGING = "CHARGING"
DISCHARGING = "DISCHARGING"
FULL = "FULL"
EMPTY = "EMPTY"

# Where the nominal cell voltage is read from the discharge OCV table.
NOMINAL_SOC_PCT = 50
NOMINAL_TEMP_C = 25


# --- 2. Data shapes -----------------------------------------------------------


@dataclass
class PackSettings:
    """Plain values the engine reads from config.yaml and passes in."""

    cells_per_module: int
    modules_in_series: int
    strings_in_parallel: int
    start_soc_pct: float
    temperature_c: float
    capacity_variation_pct: float  # +- percent of the battery file capacity
    r0_variation_pct: float  # +- percent of the battery file R0
    soc_variation_pct: float  # +- SOC percentage points around start_soc_pct
    seed: int
    deadband_a: float


@dataclass
class Module:
    cells: list[Cell]


@dataclass
class String:
    modules: list[Module]


@dataclass
class Pack:
    battery: Battery
    strings: list[String]
    temperature_c: float
    deadband_a: float
    state: str = IDLE
    hold_soc: bool = False


@dataclass
class CellPosition:
    """One cell's voltage and where it is. Both numbers start at 1."""

    voltage_v: float
    module: int
    cell: int


@dataclass
class StringResult:
    voltage_v: float
    current_a: float
    soc_pct: float


@dataclass
class PackResult:
    """Everything the engine hands to the interfaces after one tick."""

    state: str
    voltage_v: float
    current_a: float  # the current that really flowed (after deadband and blocking)
    soc_pct: float  # average of all cells
    strings: list[StringResult]
    module_cell_voltages: list[list[float]]  # index 0 is module 1
    highest_cell: CellPosition
    lowest_cell: CellPosition
    max_charge_a: float
    max_discharge_a: float
    temp_out_of_range: bool
    temperature_c: float  # the set pack temperature
    current_timeout: bool = False  # never set here; the engine sets it (REQ-10)


@dataclass
class PackInfo:
    """The shape of the pack and the highest values it can reach.

    The engine hands this to every interface at start, so an interface can check
    that the pack fits its messages before sending anything.
    """

    cells_per_module: int
    modules_in_series: int
    strings_in_parallel: int
    v_max: float  # highest cell voltage before FULL, from cell.csv
    max_charge_a: float  # P x the highest value in limit_charge.csv
    max_discharge_a: float  # P x the highest value in limit_discharge.csv


@dataclass
class BatterySummary:
    """Two numbers per battery type, for the pack summary in the web UI."""

    capacity_ah: float  # from cell.csv
    nominal_v: float  # discharge OCV at NOMINAL_SOC_PCT and NOMINAL_TEMP_C


# --- 3. Building the pack (once, at start) ------------------------------------


def build_pack(battery_folder: Path, settings: PackSettings) -> Pack:
    """Load the battery folder and make every cell with its own values."""
    battery = load_battery(battery_folder)
    generator = random.Random(settings.seed)  # same seed = same pack

    strings = []
    for _ in range(settings.strings_in_parallel):
        modules = []
        for _ in range(settings.modules_in_series):
            cells = [
                make_varied_cell(battery, settings, generator)
                for _ in range(settings.cells_per_module)
            ]
            modules.append(Module(cells=cells))
        strings.append(String(modules=modules))

    return Pack(
        battery=battery,
        strings=strings,
        temperature_c=settings.temperature_c,
        deadband_a=settings.deadband_a,
    )


def make_varied_cell(
    battery: Battery, settings: PackSettings, generator: random.Random
) -> Cell:
    """One cell spread randomly around the battery file values.

    Always three draws in the same order, so the same seed gives the same pack.
    """
    capacity_spread = generator.uniform(
        -settings.capacity_variation_pct, settings.capacity_variation_pct
    )
    r0_spread = generator.uniform(-settings.r0_variation_pct, settings.r0_variation_pct)
    soc_spread = generator.uniform(
        -settings.soc_variation_pct, settings.soc_variation_pct
    )
    return Cell(
        capacity_ah=battery.capacity_ah * (1 + capacity_spread / 100),
        r0_ohm=battery.r0_ohm * (1 + r0_spread / 100),
        soc_pct=clamp_soc(settings.start_soc_pct + soc_spread),
    )


def battery_summary(battery_folder: Path) -> BatterySummary:
    """Capacity and nominal cell voltage of a battery folder.

    Nominal voltage is taken as the discharge OCV at 50 % SOC and 25 degC.
    """
    battery = load_battery(battery_folder)
    return BatterySummary(
        capacity_ah=battery.capacity_ah,
        nominal_v=lookup(battery.ocv_discharge, NOMINAL_SOC_PCT, NOMINAL_TEMP_C),
    )


def clamp_soc(soc_pct: float) -> float:
    return max(0.0, min(100.0, soc_pct))


def cell_voltage_range(pack: Pack) -> tuple[float, float]:
    """v_min and v_max of one cell, from cell.csv. The web UI marks a cell
    at or beyond either one."""
    return pack.battery.v_min, pack.battery.v_max


def pack_info(pack: Pack) -> PackInfo:
    """Describe the pack for the interfaces: its shape and highest values."""
    battery = pack.battery
    strings_in_parallel = len(pack.strings)
    first_string = pack.strings[0]
    highest_charge = max(max(row) for row in battery.limit_charge.values)
    highest_discharge = max(max(row) for row in battery.limit_discharge.values)
    return PackInfo(
        cells_per_module=len(first_string.modules[0].cells),
        modules_in_series=len(first_string.modules),
        strings_in_parallel=strings_in_parallel,
        v_max=battery.v_max,
        max_charge_a=strings_in_parallel * highest_charge,
        max_discharge_a=strings_in_parallel * highest_discharge,
    )


# --- 4. Each tick -------------------------------------------------------------


def tick(pack: Pack, current_a: float, dt_s: float) -> PackResult:
    """Move the pack forward by dt_s seconds with this pack current."""
    # Step 1: deadband. Small noise counts as 0 A.
    if abs(current_a) <= pack.deadband_a:
        current_a = 0.0

    # Step 2: state changes caused by the current direction.
    pack.state = state_after_current(pack.state, current_a)

    # Step 3: FULL blocks charging, EMPTY blocks discharging (contactors open).
    if pack.state == FULL and current_a < 0:
        current_a = 0.0
    if pack.state == EMPTY and current_a > 0:
        current_a = 0.0

    # Step 4: split the pack current across the strings.
    string_currents = split_current(pack, current_a)

    # Step 5: update every cell with its own string's current. The pack
    # current picks the OCV curve. Hold SOC moves time by 0 s, so SOC
    # stays put but the curve still follows the current.
    cell_dt_s = 0.0 if pack.hold_soc else dt_s
    for string, string_current_a in zip(pack.strings, string_currents, strict=True):
        for cell in cells_of_string(string):
            update_cell(
                cell, string_current_a, cell_dt_s, direction_current_a=current_a
            )

    # Step 6: work out the results, then the state changes caused by voltage.
    result = make_result(pack, current_a, string_currents)
    pack.state = state_after_voltage(
        pack.state,
        result.lowest_cell.voltage_v,
        result.highest_cell.voltage_v,
        pack.battery,
    )
    result.state = pack.state
    return result


# --- 5. State machine ---------------------------------------------------------
# The state table in PROJECT.md, split in two: the rows about current
# direction run before the current flows, the rows about cell voltage after.


def state_after_current(state: str, current_a: float) -> str:
    """Rows of the state table that depend on the current. Others keep the state."""
    if state in (IDLE, CHARGING) and current_a > 0:
        return DISCHARGING
    if state in (IDLE, DISCHARGING) and current_a < 0:
        return CHARGING
    if state in (CHARGING, DISCHARGING) and current_a == 0:
        return IDLE
    if state == EMPTY and current_a < 0:
        return CHARGING
    if state == FULL and current_a > 0:
        return DISCHARGING
    return state


def state_after_voltage(
    state: str, lowest_cell_v: float, highest_cell_v: float, battery: Battery
) -> str:
    """Rows of the state table that depend on cell voltage."""
    if state == DISCHARGING and lowest_cell_v <= battery.v_min:
        return EMPTY
    if state == CHARGING and highest_cell_v >= battery.v_max:
        return FULL
    return state


# --- 6. Current split ---------------------------------------------------------


def split_current(pack: Pack, pack_current_a: float) -> list[float]:
    """Share the pack current between strings by resistance.

    All strings sit at one pack voltage. A string with a higher OCV or a lower
    resistance gives more current. Each string k:
        V_pack = (sum of OCV_k / R_k  -  I_pack) / (sum of 1 / R_k)
        I_k    = (OCV_k - V_pack) / R_k
    The I_k always add up to I_pack. At I_pack = 0, strings at different SOC
    still exchange a circulating current.
    """
    string_ocvs = []
    string_resistances = []
    for string in pack.strings:
        cells = cells_of_string(string)
        string_ocvs.append(
            sum(
                open_circuit_voltage(c, pack.battery, pack.temperature_c) for c in cells
            )
        )
        string_resistances.append(sum(c.r0_ohm for c in cells))

    # With no resistance the formula divides by 0: fall back to an equal split.
    if any(r == 0 for r in string_resistances):
        return [pack_current_a / len(pack.strings)] * len(pack.strings)

    sum_ocv_over_r = sum(
        ocv / r for ocv, r in zip(string_ocvs, string_resistances, strict=True)
    )
    sum_one_over_r = sum(1 / r for r in string_resistances)
    pack_voltage = (sum_ocv_over_r - pack_current_a) / sum_one_over_r

    return [
        (ocv - pack_voltage) / r
        for ocv, r in zip(string_ocvs, string_resistances, strict=True)
    ]


def cells_of_string(string: String) -> list[Cell]:
    """All cells of one string, module by module."""
    return [cell for module in string.modules for cell in module.cells]


# --- 7. Results ---------------------------------------------------------------


def make_result(
    pack: Pack, pack_current_a: float, string_currents: list[float]
) -> PackResult:
    """Voltages, SOC, highest and lowest cell and limits after this tick."""
    battery = pack.battery
    temp_c = pack.temperature_c

    string_results = []
    module_cell_voltages = []
    highest = None
    lowest = None
    module_number = 0
    for string, string_current_a in zip(pack.strings, string_currents, strict=True):
        string_voltage = 0.0
        for module in string.modules:
            module_number += 1  # numbered string by string
            voltages = []
            for cell_number, cell in enumerate(module.cells, start=1):
                v = cell_voltage(cell, battery, string_current_a, temp_c)
                voltages.append(v)
                if highest is None or v > highest.voltage_v:
                    highest = CellPosition(v, module_number, cell_number)
                if lowest is None or v < lowest.voltage_v:
                    lowest = CellPosition(v, module_number, cell_number)
            module_cell_voltages.append(voltages)
            string_voltage += sum(voltages)

        cells = cells_of_string(string)
        string_results.append(
            StringResult(
                voltage_v=string_voltage,
                current_a=string_current_a,
                soc_pct=sum(c.soc_pct for c in cells) / len(cells),
            )
        )

    # Pack limits: every string must stay within its weakest cell's limit.
    all_cells = cells_of_pack(pack)
    limits = [current_limits(cell, battery, temp_c) for cell in all_cells]
    strings_in_parallel = len(pack.strings)

    return PackResult(
        state=pack.state,
        voltage_v=sum(s.voltage_v for s in string_results) / len(string_results),
        current_a=pack_current_a,
        soc_pct=average_soc(pack),
        strings=string_results,
        module_cell_voltages=module_cell_voltages,
        highest_cell=highest,
        lowest_cell=lowest,
        max_charge_a=strings_in_parallel * min(lim.charge_a for lim in limits),
        max_discharge_a=strings_in_parallel * min(lim.discharge_a for lim in limits),
        temp_out_of_range=any(lim.temp_out_of_range for lim in limits),
        temperature_c=temp_c,
    )


def cells_of_pack(pack: Pack) -> list[Cell]:
    """All cells of the pack, string by string."""
    return [cell for string in pack.strings for cell in cells_of_string(string)]


def module_cell_socs(pack: Pack) -> list[list[float]]:
    """SOC of every cell, one list per module, numbered string by string:
    the same shape as PackResult.module_cell_voltages."""
    return [
        [cell.soc_pct for cell in module.cells]
        for string in pack.strings
        for module in string.modules
    ]


def pack_capacity_ah(pack: Pack) -> float:
    """Nominal pack capacity: strings in parallel x the capacity in cell.csv.
    The same number the pack summary in the web UI shows."""
    return len(pack.strings) * pack.battery.capacity_ah


def average_soc(pack: Pack) -> float:
    cells = cells_of_pack(pack)
    return sum(c.soc_pct for c in cells) / len(cells)


# --- 8. Test controls (REQ-21) ------------------------------------------------


def set_soc(pack: Pack, soc_pct: float) -> None:
    """Jump the pack to soc_pct. Each cell keeps its offset from the average."""
    offset_to_target = soc_pct - average_soc(pack)
    for cell in cells_of_pack(pack):
        cell.soc_pct = clamp_soc(cell.soc_pct + offset_to_target)


def set_hold_soc(pack: Pack, on: bool) -> None:
    """Freeze SOC while current keeps flowing. Voltages still show I x R0."""
    pack.hold_soc = on
