"""CAN interface: sends the pack result on a CAN bus and reads the current command.

Follows the interface contract in PROJECT.md: start, send, read_current, stop.

Two kinds of frames go out:
- Pack messages, defined entirely by the DBC file (REQ-12). A message is sent
  when all its signals use the emulator's names below, at its own
  GenMsgCycleTime.
- Module frames with cell voltages and module status, following the
  module_frames pattern in config.yaml (REQ-13), because they depend on the
  pack size.

One kind of frame comes in: the DBC message holding CurrentCommand.
read_current() returns its newest value. The timeout is not handled here but
in engine.py, the same for every interface (REQ-10).

The engine calls send() once per tick, every 100 ms of emulated time, so a
message with a 100 ms cycle goes out every tick and one with 1000 ms every 10th.

send() never stops the emulator: values outside a signal's range are clamped
(REQ-27) and frames the bus refuses are dropped with a warning (REQ-28).

Units: DBC physical values are in V, A and %; cell voltages in module frames
are whole millivolts.
"""

import math
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

import can
import cantools

from emulator.pack import PackInfo, PackResult

# --- 1. Constants and signal names --------------------------------------------

TICK_MS = 100  # the engine calls send() once per tick of 100 ms (contract)
DRIVERS = ("socketcan", "virtual")
MAX_STANDARD_ID = 0x7FF
STATUS_OFFSET = 0xF  # module temperature and status frame, last ID of a module
FRAME_LENGTH = 8  # every module frame has DLC 8
UNUSED_BYTE = 0xFF
MAX_CELLS_PER_FRAME = 4  # 4 cells x 2 bytes = 8 bytes
UINT16_RANGE = (0, 65535)  # module frame cell voltages, mV
INT16_RANGE = (-32768, 32767)  # module frame temperature, 0.1 degC
ALIVE_COUNTER_WRAP = 256  # counts 0 to 255, then starts at 0 again
PACK_STATES = ("IDLE", "CHARGING", "DISCHARGING", "FULL", "EMPTY")

# Signals the emulator fills in. Their values come from output_values() below.
OUTPUT_SIGNALS = (
    "PackVoltage",
    "PackCurrent",
    "PackSOC",
    "PackState",
    "CurrentTimeout",
    "TempOutOfRange",
    "AliveCounter",
    "CellVmax",
    "CellVmaxModule",
    "CellVmaxCell",
    "CellVmin",
    "CellVminModule",
    "CellVminCell",
    "MaxChargeCurrent",
    "MaxDischargeCurrent",
)

# Signals the emulator receives, never sends.
COMMAND_SIGNAL = "CurrentCommand"  # amps, discharge positive
INPUT_SIGNALS = (COMMAND_SIGNAL,)


def output_values(result: PackResult, alive_counter: int) -> dict:
    """The value of every output signal for this result, by signal name."""
    return {
        "PackVoltage": result.voltage_v,
        "PackCurrent": result.current_a,
        "PackSOC": result.soc_pct,
        "PackState": result.state,  # sent as its VAL_ name, e.g. "IDLE"
        "CurrentTimeout": int(result.current_timeout),  # set by the engine
        "TempOutOfRange": int(result.temp_out_of_range),
        "AliveCounter": alive_counter,
        "CellVmax": result.highest_cell.voltage_v,
        "CellVmaxModule": result.highest_cell.module,
        "CellVmaxCell": result.highest_cell.cell,
        "CellVmin": result.lowest_cell.voltage_v,
        "CellVminModule": result.lowest_cell.module,
        "CellVminCell": result.lowest_cell.cell,
        "MaxChargeCurrent": result.max_charge_a,
        "MaxDischargeCurrent": result.max_discharge_a,
    }


# --- 2. Data shapes -----------------------------------------------------------


class CanSettingsError(ValueError):
    """A problem with interfaces.can in config.yaml or with the DBC file."""


@dataclass
class OutputMessage:
    """A DBC message the emulator sends, and how often (in ticks)."""

    message: cantools.database.Message
    every_ticks: int


@dataclass
class ModuleFrames:
    """The module_frames settings from config.yaml, checked."""

    base_id: int
    id_step: int
    cells_per_frame: int
    every_ticks: int


@dataclass
class CanState:
    """Everything the CAN interface keeps between calls."""

    bus: can.BusABC
    messages: list[OutputMessage]
    module_frames: ModuleFrames
    command_message: cantools.database.Message | None  # None: DBC has no input
    tick_count: int = 0
    alive_counter: int = 0
    warned: set[str] = field(default_factory=set)  # warnings already printed
    send_failing: bool = False  # the bus refused the last frame (REQ-28)


state: CanState | None = None  # set by start(), cleared by stop()


# --- 3. start: settings, checks, then open the bus ----------------------------


def start(settings: dict, pack_info: PackInfo) -> bool:
    """Check everything first, then open the bus. Nothing is sent here.

    Returns True if the DBC has a CurrentCommand signal, so this interface can
    provide current commands; otherwise False (interface contract).
    """
    global state

    driver = settings.get("driver", "socketcan")
    if driver not in DRIVERS:
        raise CanSettingsError(
            f"interfaces.can.driver must be one of {', '.join(DRIVERS)}"
        )
    channel = settings.get("channel")
    if not isinstance(channel, str) or not channel:
        raise CanSettingsError("interfaces.can.channel is missing")
    bitrate = whole_number(settings, "bitrate", "interfaces.can.bitrate", minimum=1)

    database = load_dbc(settings.get("dbc"))
    messages = check_dbc(database, pack_info)
    module_frames = check_module_frames(
        settings.get("module_frames"), pack_info, database
    )

    state = CanState(
        bus=open_bus(driver, channel, bitrate),
        messages=messages,
        module_frames=module_frames,
        command_message=find_command_message(database),
    )
    # Contract: True if this interface can provide current commands, which
    # needs a DBC message with a CurrentCommand signal.
    return state.command_message is not None


def whole_number(values: dict, key: str, name: str, minimum: int = 0) -> int:
    """values[key] as a whole number of at least `minimum`, or a clear error."""
    value = values.get(key)
    if value is None:
        raise CanSettingsError(f"{name} is missing")
    if isinstance(value, bool) or not isinstance(value, int):
        raise CanSettingsError(f"{name} must be a whole number")
    if value < minimum:
        raise CanSettingsError(f"{name} must be at least {minimum}")
    return int(value)


def open_bus(driver: str, channel: str, bitrate: int) -> can.BusABC:
    """Open the bus. SocketCAN takes its bitrate from `ip link`, not from here."""
    try:
        return can.Bus(interface=driver, channel=channel, bitrate=bitrate)
    except (can.CanError, OSError) as error:
        raise CanSettingsError(
            f"interfaces.can.channel: cannot open {channel} with {driver} ({error}). "
            "See README 'Run it' for setting up vcan0."
        ) from None


# --- 4. start: checking the DBC file ------------------------------------------


def load_dbc(path) -> cantools.database.Database:
    if not path:
        raise CanSettingsError("interfaces.can.dbc is missing")
    path = Path(path)
    if not path.is_file():
        raise CanSettingsError(f"interfaces.can.dbc: file not found: {path}")
    try:
        return cantools.database.load_file(path)
    except cantools.database.Error as error:  # any DBC parse error
        raise CanSettingsError(
            f"interfaces.can.dbc: cannot read {path}: {error}"
        ) from None


def check_dbc(
    database: cantools.database.Database, pack_info: PackInfo
) -> list[OutputMessage]:
    """Check every message and return the ones the emulator sends."""
    worst_cases = worst_case_values(pack_info)
    messages = []
    for message in database.messages:
        where = f"message {message.name} (0x{message.frame_id:X})"
        names = [signal.name for signal in message.signals]

        for name in names:
            if name not in OUTPUT_SIGNALS and name not in INPUT_SIGNALS:
                raise CanSettingsError(
                    f"interfaces.can.dbc: signal {name} in {where} is not a name "
                    f"the emulator knows. Known names: "
                    f"{', '.join(OUTPUT_SIGNALS + INPUT_SIGNALS)}"
                )

        outputs = [name for name in names if name in OUTPUT_SIGNALS]
        if not outputs:
            continue  # an input message, see find_command_message()
        if len(outputs) != len(names):
            raise CanSettingsError(
                f"interfaces.can.dbc: {where} mixes signals the emulator sends "
                f"with signals it receives"
            )

        cycle_ms = message.cycle_time
        if not cycle_ms or cycle_ms % TICK_MS != 0:
            raise CanSettingsError(
                f"interfaces.can.dbc: {where} needs a GenMsgCycleTime that is a "
                f"multiple of {TICK_MS} ms, got {cycle_ms}"
            )

        for signal in message.signals:
            check_signal(signal, where, worst_cases)

        messages.append(OutputMessage(message, every_ticks=cycle_ms // TICK_MS))
    return messages


def check_signal(signal, where: str, worst_cases: dict) -> None:
    """PackState needs all state names; other signals must fit their values."""
    if signal.name == "PackState":
        names = {str(value) for value in (signal.choices or {}).values()}
        missing = [state_name for state_name in PACK_STATES if state_name not in names]
        if missing:
            raise CanSettingsError(
                f"interfaces.can.dbc: signal PackState in {where} needs VAL_ names "
                f"for {', '.join(missing)}"
            )
        return

    if signal.name not in worst_cases:
        return  # PackCurrent: any value can come in, clamped when sending (REQ-27)
    lowest, highest = worst_cases[signal.name]
    signal_low, signal_high = signal_range(signal)
    if lowest < signal_low or highest > signal_high:
        raise CanSettingsError(
            f"interfaces.can.dbc: signal {signal.name} in {where} covers "
            f"{signal_low:g} to {signal_high:g}, but this pack can reach "
            f"{lowest:g} to {highest:g}"
        )


def signal_range(signal) -> tuple[float, float]:
    """Lowest and highest physical value: the DBC [min|max], else from the bits."""
    if signal.is_signed:
        raw_low = -(2 ** (signal.length - 1))
        raw_high = 2 ** (signal.length - 1) - 1
    else:
        raw_low = 0
        raw_high = 2**signal.length - 1
    low = raw_low * signal.scale + signal.offset
    high = raw_high * signal.scale + signal.offset
    if signal.scale < 0:
        low, high = high, low
    if signal.minimum is not None:
        low = signal.minimum
    if signal.maximum is not None:
        high = signal.maximum
    return low, high


def find_command_message(
    database: cantools.database.Database,
) -> cantools.database.Message | None:
    """The DBC message holding CurrentCommand, or None if the DBC has none."""
    for message in database.messages:
        if any(signal.name == COMMAND_SIGNAL for signal in message.signals):
            return message
    return None


# --- 5. start: checking ranges ------------------------------------------------


def worst_case_values(pack_info: PackInfo) -> dict:
    """Lowest and highest value each output signal can take with this pack."""
    cells_in_series = pack_info.cells_per_module * pack_info.modules_in_series
    module_count = pack_info.modules_in_series * pack_info.strings_in_parallel
    return {
        "PackVoltage": (0, cells_in_series * pack_info.v_max),
        "PackSOC": (0, 100),
        "CurrentTimeout": (0, 1),
        "TempOutOfRange": (0, 1),
        "AliveCounter": (0, ALIVE_COUNTER_WRAP - 1),
        "CellVmax": (0, pack_info.v_max),
        "CellVmaxModule": (1, module_count),
        "CellVmaxCell": (1, pack_info.cells_per_module),
        "CellVmin": (0, pack_info.v_max),
        "CellVminModule": (1, module_count),
        "CellVminCell": (1, pack_info.cells_per_module),
        "MaxChargeCurrent": (0, pack_info.max_charge_a),
        "MaxDischargeCurrent": (0, pack_info.max_discharge_a),
    }


# --- 6. start: checking module frames -----------------------------------------


def check_module_frames(
    values, pack_info: PackInfo, database: cantools.database.Database
) -> ModuleFrames:
    """Module frames must fit inside their module's IDs and not hit DBC IDs."""
    name = "interfaces.can.module_frames"
    if not isinstance(values, dict):
        raise CanSettingsError(f"{name} is missing")
    base_id = whole_number(values, "base_id", f"{name}.base_id")
    id_step = whole_number(values, "id_step", f"{name}.id_step", minimum=1)
    cells_per_frame = whole_number(
        values, "cells_per_frame", f"{name}.cells_per_frame", minimum=1
    )
    cycle_ms = whole_number(values, "cycle_ms", f"{name}.cycle_ms", minimum=TICK_MS)

    if cells_per_frame > MAX_CELLS_PER_FRAME:
        raise CanSettingsError(
            f"{name}.cells_per_frame must be at most {MAX_CELLS_PER_FRAME} "
            f"(2 bytes per cell, 8 bytes per frame)"
        )
    if cycle_ms % TICK_MS != 0:
        raise CanSettingsError(f"{name}.cycle_ms must be a multiple of {TICK_MS}")

    frames = math.ceil(pack_info.cells_per_module / cells_per_frame)
    if frames > STATUS_OFFSET:
        raise CanSettingsError(
            f"{name}: {pack_info.cells_per_module} cells at {cells_per_frame} per "
            f"frame need {frames} frames, but only {STATUS_OFFSET} fit before the "
            f"status frame at offset 0x{STATUS_OFFSET:X}"
        )
    if id_step <= STATUS_OFFSET:
        raise CanSettingsError(
            f"{name}.id_step 0x{id_step:X} must be at least 0x{STATUS_OFFSET + 1:X}: "
            f"module 1's status frame 0x{base_id + STATUS_OFFSET:X} would overlap "
            f"module 2's IDs from 0x{base_id + id_step:X}"
        )

    module_count = pack_info.modules_in_series * pack_info.strings_in_parallel
    last_id = base_id + (module_count - 1) * id_step + STATUS_OFFSET
    if last_id > MAX_STANDARD_ID:
        raise CanSettingsError(
            f"{name}: {module_count} modules need IDs up to 0x{last_id:X}, "
            f"above the highest standard ID 0x{MAX_STANDARD_ID:X}"
        )

    module_ids = set()
    for module in range(1, module_count + 1):
        module_ids.update(module_frame_ids(base_id, id_step, module, frames))
    for message in database.messages:
        if message.frame_id in module_ids:
            raise CanSettingsError(
                f"{name}: module frames use 0x{message.frame_id:X}, the ID of DBC "
                f"message {message.name}"
            )

    return ModuleFrames(
        base_id=base_id,
        id_step=id_step,
        cells_per_frame=cells_per_frame,
        every_ticks=cycle_ms // TICK_MS,
    )


def module_frame_ids(base_id: int, id_step: int, module: int, frames: int) -> list:
    """Every ID of one module (module numbered from 1): cell frames, then status."""
    first = base_id + (module - 1) * id_step
    return [first + n for n in range(frames)] + [first + STATUS_OFFSET]


# --- 7. send: DBC messages, then module frames --------------------------------


def send(result: PackResult) -> None:
    """Send every message whose cycle time is due this tick."""
    if state is None:
        raise RuntimeError("can_bus.send() called before start()")

    for output in state.messages:
        if state.tick_count % output.every_ticks == 0:
            send_message(output.message, result)

    if state.tick_count % state.module_frames.every_ticks == 0:
        send_module_frames(result)

    state.tick_count += 1


def send_message(message: cantools.database.Message, result: PackResult) -> None:
    """Encode one DBC message from the result by signal name and send it.

    Every number is first clamped to its signal's range (REQ-27). PackState is
    a VAL_ name like "IDLE", not a number, so it is left as it is.
    """
    all_values = output_values(result, state.alive_counter)
    values = {}
    for signal in message.signals:
        value = all_values[signal.name]
        if signal.name != "PackState":
            low, high = signal_range(signal)
            value = clamp(signal.name, value, low, high)
        values[signal.name] = value

    if "AliveCounter" in values:
        state.alive_counter = (state.alive_counter + 1) % ALIVE_COUNTER_WRAP

    try:
        data = message.encode(values, strict=True)
    except cantools.database.EncodeError as error:
        # Should not happen after clamping. If it does, skip this message
        # instead of stopping the emulator.
        warn_once(message.name, f"cannot encode {message.name}, not sent: {error}")
        return

    bus_send(
        can.Message(
            arbitration_id=message.frame_id,
            is_extended_id=message.is_extended_frame,
            data=data,
        )
    )


# --- 8. send: module frames (REQ-13) ------------------------------------------
# Modules are numbered string by string. For module m, frame n holds cells
# n x k + 1 to n x k + k (k = cells_per_frame) as uint16 millivolts, little
# endian, at base_id + (m - 1) x id_step + n. Offset 0xF holds the module
# temperature (int16, 0.1 degC) and a flags byte (bit 0: temperature out of
# range). Every frame is 8 bytes; unused bytes are 0xFF.


def send_module_frames(result: PackResult) -> None:
    frames = state.module_frames
    k = frames.cells_per_frame
    for module, voltages in enumerate(result.module_cell_voltages, start=1):
        ids = module_frame_ids(
            frames.base_id, frames.id_step, module, math.ceil(len(voltages) / k)
        )
        cell_frame_ids, status_id = ids[:-1], ids[-1]

        for n, frame_id in enumerate(cell_frame_ids):
            cells = voltages[n * k : (n + 1) * k]
            millivolts = [
                clamp("module cell voltage (mV)", round(v * 1000), *UINT16_RANGE)
                for v in cells
            ]
            send_frame(frame_id, struct.pack(f"<{len(cells)}H", *millivolts))

        flags = 1 if result.temp_out_of_range else 0
        temperature = clamp(
            "module temperature (0.1 degC)",
            round(result.temperature_c * 10),
            *INT16_RANGE,
        )
        send_frame(status_id, struct.pack("<hB", temperature, flags))


def pad_to_8(data: bytes) -> bytes:
    """Fill the frame up to 8 bytes with 0xFF."""
    return data + bytes([UNUSED_BYTE] * (FRAME_LENGTH - len(data)))


def send_frame(frame_id: int, data: bytes) -> None:
    bus_send(
        can.Message(arbitration_id=frame_id, is_extended_id=False, data=pad_to_8(data))
    )


# --- 9. send: never stopping the emulator (REQ-27, REQ-28) --------------------
# Warnings go to stderr, so they do not mix with the status lines on stdout.


def clamp(name: str, value, low, high):
    """value limited to low..high. The first time a name is clamped, warn."""
    if low <= value <= high:
        return value
    clamped = max(low, min(high, value))
    warn_once(
        name,
        f"{name} {value:g} is outside {low:g} to {high:g}, sent as {clamped:g} "
        f"(warned once)",
    )
    return clamped


def warn_once(key: str, text: str) -> None:
    """Print a warning the first time for this key, never again until restart."""
    if key not in state.warned:
        state.warned.add(key)
        print(f"Warning: interfaces.can: {text}", file=sys.stderr)


def bus_send(message: can.Message) -> None:
    """Send one frame. If the bus refuses it, drop it and keep running.

    Warns once when sending starts failing and once when it works again, not
    on every frame (10 or more per tick would flood the terminal).
    """
    try:
        state.bus.send(message)
    except can.CanError as error:
        if not state.send_failing:
            state.send_failing = True
            print(
                f"Warning: interfaces.can: the bus refused a frame, frames are "
                f"dropped until it works again ({error})",
                file=sys.stderr,
            )
        return
    if state.send_failing:
        state.send_failing = False
        print("interfaces.can: sending works again", file=sys.stderr)


# --- 10. read_current and stop ------------------------------------------------


def read_current() -> float | None:
    """The newest CurrentCommand in amps, or None if nothing new arrived.

    Reads every frame waiting on the bus without blocking, so frames never
    pile up. Frames with other IDs are ignored. If several commands arrived
    since the last call, only the newest counts. A command frame that does not
    decode (for example too short) is skipped with a warning (REQ-27).
    """
    if state is None:
        raise RuntimeError("can_bus.read_current() called before start()")

    command = state.command_message
    newest = None
    while (frame := state.bus.recv(timeout=0)) is not None:
        if command is None:
            continue  # the DBC has no input message: just empty the queue
        if frame.arbitration_id != command.frame_id:
            continue
        if frame.is_extended_id != command.is_extended_frame:
            continue
        try:
            decoded = command.decode(frame.data, decode_choices=False)
        except cantools.database.DecodeError as error:
            warn_once(
                "received " + command.name,
                f"skipped a {command.name} frame that does not decode "
                f"(warned once): {error}",
            )
            continue
        newest = float(decoded[COMMAND_SIGNAL])
    return newest


def stop() -> None:
    """Close the bus. Safe to call more than once."""
    global state
    if state is not None:
        state.bus.shutdown()
        state = None
