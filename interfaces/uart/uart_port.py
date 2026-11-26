"""UART interface: sends the pack result as text lines and reads the current command.

Follows the interface contract in docs/engineering/design.md: start, send,
read_current, stop.

Every line is ASCII text like $PACK,312.45,50.0,80.00,DISCHARGING,0,0*7A, ending
in \\r\\n. The two hex digits after * are the checksum: the XOR of every character
between $ and * (see docs/interfaces/uart.md).

Sent every cycle_ms: $PACK, $CELL, $LIM and, if modules is true, one $MOD line
per module (REQ-39). Received: $CUR,<amps> sets the current (REQ-40).
read_current() returns its newest value. The timeout is not handled here but
in engine.py, the same for every interface (REQ-10).

The engine calls send() once per tick, every 100 ms of emulated time, so with
cycle_ms 1000 the lines go out every 10th tick.

Nothing here stops the emulator once it runs: bad input lines are ignored
with a warning (REQ-40), and if the port fails it is closed and reopened every
2 seconds until it works again (REQ-41).

Units: V, A and % like the pack result; cell voltages in $MOD lines are whole
millivolts.
"""

import re
import string
import sys
from dataclasses import dataclass, field

import serial

from emulator.pack import (
    CHARGING,
    DISCHARGING,
    EMPTY,
    FULL,
    IDLE,
    PackInfo,
    PackResult,
)

# --- 1. Constants -------------------------------------------------------------

TICK_MS = 100  # the engine calls send() once per tick of 100 ms (contract)
MAX_LINE = 128  # received lines longer than this are ignored, without \r\n
REOPEN_TICKS = 20  # try to reopen a lost port every 2 s (--speed is 1, REQ-26)
WRITE_TIMEOUT_S = 0.05  # one write never holds up the 100 ms loop for long
BITS_PER_BYTE = 10  # on the wire: start bit, 8 data bits, stop bit
MAX_LINE_USE = 0.8  # lines may use at most 80 % of the line in one cycle (REQ-42)
LINE_END = "\r\n"
# An empty line after every cycle, so a serial terminal shows each cycle as a block.
CYCLE_END = LINE_END

# Defaults for settings missing from interfaces.uart in config.yaml.
DEFAULT_PORT = "/dev/ttyUSB0"  # a USB serial adapter; /dev/serial0 is the Pi's own UART
DEFAULT_BAUDRATE = 115200
DEFAULT_CYCLE_MS = 1000
DEFAULT_MODULES = False

PACK_STATES = (IDLE, CHARGING, DISCHARGING, FULL, EMPTY)
OWN_TYPES = ("PACK", "CELL", "LIM", "MOD")  # lines this interface sends
COMMAND_TYPE = "CUR"  # the line it receives
NUMBER = re.compile(r"-?\d+(\.\d+)?")  # like 50, -20 or 12.5


# --- 2. Data shapes -----------------------------------------------------------


class UartSettingsError(ValueError):
    """A problem with interfaces.uart in config.yaml or with the port."""


@dataclass
class UartSettings:
    """The interfaces.uart settings from config.yaml, checked."""

    port: str
    baudrate: int
    cycle_ms: int
    modules: bool


@dataclass
class UartState:
    """Everything the UART interface keeps between calls."""

    settings: UartSettings
    port: serial.SerialBase | None  # None while the port is lost (REQ-41)
    every_ticks: int  # lines go out every this many ticks
    tick_count: int = 0
    buffer: bytes = b""  # the start of a line whose end has not arrived yet
    skipping: bool = False  # dropping the rest of a line that is too long
    warned: set[str] = field(default_factory=set)  # warnings already printed
    failing: bool = False  # the port failed and has not worked since (REQ-41)
    lost_tick: int = 0  # tick_count when the port was lost or last tried


state: UartState | None = None  # set by start(), cleared by stop()


# --- 3. start: settings, line budget, then open the port ----------------------


def start(settings: dict, pack_info: PackInfo) -> bool:
    """Check everything first, then open the port. Nothing is sent here.

    Returns True: a UART can always provide current commands ($CUR lines).
    """
    global state

    checked = check_settings(settings)
    check_line_budget(checked, pack_info)
    try:
        port = open_port(checked)
    except serial.SerialException as error:
        raise UartSettingsError(
            f"interfaces.uart.port: cannot open {checked.port} ({error}). Check the "
            "adapter is plugged in and that your user is in the dialout group."
        ) from None

    state = UartState(
        settings=checked, port=port, every_ticks=checked.cycle_ms // TICK_MS
    )
    return True


def check_settings(settings: dict) -> UartSettings:
    """Every setting, with its default if it is missing."""
    port = settings.get("port", DEFAULT_PORT)
    if not isinstance(port, str) or not port:
        raise UartSettingsError(
            "interfaces.uart.port must be a port like /dev/serial0 or /dev/ttyUSB0"
        )

    baudrate = settings.get("baudrate", DEFAULT_BAUDRATE)
    if isinstance(baudrate, bool) or not isinstance(baudrate, int) or baudrate < 1:
        raise UartSettingsError(
            "interfaces.uart.baudrate must be a whole number like 115200"
        )

    cycle_ms = settings.get("cycle_ms", DEFAULT_CYCLE_MS)
    if (
        isinstance(cycle_ms, bool)
        or not isinstance(cycle_ms, int)
        or cycle_ms < TICK_MS
        or cycle_ms % TICK_MS != 0
    ):
        raise UartSettingsError(
            f"interfaces.uart.cycle_ms must be a whole multiple of {TICK_MS}, like 1000"
        )

    modules = settings.get("modules", DEFAULT_MODULES)
    if not isinstance(modules, bool):
        raise UartSettingsError("interfaces.uart.modules must be true or false")

    return UartSettings(
        port=port, baudrate=int(baudrate), cycle_ms=int(cycle_ms), modules=modules
    )


def check_line_budget(settings: UartSettings, pack_info: PackInfo) -> None:
    """REQ-42: the lines of one cycle must fit in 80 % of what the baudrate
    carries in cycle_ms, so the port never falls behind."""
    needed = bytes_per_cycle(settings, pack_info)
    available = settings.baudrate / BITS_PER_BYTE * settings.cycle_ms / 1000
    allowed = available * MAX_LINE_USE
    if needed > allowed:
        raise UartSettingsError(
            f"interfaces.uart.baudrate: {settings.baudrate} baud carries "
            f"{int(allowed)} bytes in {MAX_LINE_USE:.0%} of cycle_ms "
            f"{settings.cycle_ms}, but this pack sends up to {needed} bytes per "
            f"cycle. Raise baudrate or cycle_ms, or set modules: false."
        )


def bytes_per_cycle(settings: UartSettings, pack_info: PackInfo) -> int:
    """The most bytes one cycle can send with this pack.

    Every line is built with the longest values the pack can give: highest
    pack voltage, the largest limit as a charge current (with its minus
    sign), SOC 100.00, the longest state name, the highest module and cell
    numbers. The empty line that ends the cycle counts too. A commanded
    current above the limits only adds a character or two, which the 20 %
    reserve covers.
    """
    cells_in_series = pack_info.cells_per_module * pack_info.modules_in_series
    module_count = pack_info.modules_in_series * pack_info.strings_in_parallel
    largest_a = max(pack_info.max_charge_a, pack_info.max_discharge_a)
    longest_state = max(PACK_STATES, key=len)
    v_max = pack_info.v_max
    last_cell = pack_info.cells_per_module

    lines = [
        make_line(
            pack_body(cells_in_series * v_max, -largest_a, 100, longest_state, 1, 1)
        ),
        make_line(
            cell_body(v_max, module_count, last_cell, v_max, module_count, last_cell)
        ),
        make_line(lim_body(largest_a, largest_a)),
    ]
    if settings.modules:
        millivolts = [round(v_max * 1000)] * pack_info.cells_per_module
        for module in range(1, module_count + 1):
            lines.append(make_line(mod_body(module, millivolts)))
    lines.append(CYCLE_END)
    return sum(len(line) for line in lines)


def open_port(settings: UartSettings) -> serial.SerialBase:
    """Open the port without blocking reads. serial_for_url takes device
    paths like /dev/ttyUSB0 and pyserial's loop:// test port alike."""
    return serial.serial_for_url(
        settings.port,
        baudrate=settings.baudrate,
        timeout=0,  # read() returns at once with whatever has arrived
        write_timeout=WRITE_TIMEOUT_S,
    )


# --- 4. Building lines (REQ-39) -----------------------------------------------
# One function per line type takes plain values, so send() and the line
# budget at start build exactly the same text.


def checksum(body: str) -> int:
    """XOR of every character between $ and *."""
    result = 0
    for character in body:
        result ^= ord(character)
    return result


def make_line(body: str) -> str:
    """$body*CS\\r\\n, CS as two upper case hex digits."""
    return f"${body}*{checksum(body):02X}{LINE_END}"


def number(value: float, decimals: int) -> str:
    """The value with a fixed number of decimals. Adding 0.0 turns -0.0 into
    0.0, so a tiny negative value never shows as "-0.0"."""
    return f"{round(value, decimals) + 0.0:.{decimals}f}"


def pack_body(voltage_v, current_a, soc_pct, state_name, timeout, temp_out) -> str:
    return (
        f"PACK,{number(voltage_v, 2)},{number(current_a, 1)},{number(soc_pct, 2)},"
        f"{state_name},{int(timeout)},{int(temp_out)}"
    )


def cell_body(low_v, low_module, low_cell, high_v, high_module, high_cell) -> str:
    return (
        f"CELL,{number(low_v, 3)},{low_module},{low_cell},"
        f"{number(high_v, 3)},{high_module},{high_cell}"
    )


def lim_body(max_charge_a, max_discharge_a) -> str:
    return f"LIM,{number(max_charge_a, 1)},{number(max_discharge_a, 1)}"


def mod_body(module: int, millivolts: list[int]) -> str:
    return ",".join(["MOD", str(module), *(str(mv) for mv in millivolts)])


def pack_lines(result: PackResult, modules: bool) -> list[str]:
    """Every line of one cycle, in the order they are sent, then the empty
    line that ends the cycle."""
    low, high = result.lowest_cell, result.highest_cell
    lines = [
        make_line(
            pack_body(
                result.voltage_v,
                result.current_a,
                result.soc_pct,
                result.state,
                result.current_timeout,  # set by the engine
                result.temp_out_of_range,
            )
        ),
        make_line(
            cell_body(
                low.voltage_v,
                low.module,
                low.cell,
                high.voltage_v,
                high.module,
                high.cell,
            )
        ),
        make_line(lim_body(result.max_charge_a, result.max_discharge_a)),
    ]
    if modules:
        for module, voltages in enumerate(result.module_cell_voltages, start=1):
            millivolts = [round(v * 1000) for v in voltages]
            lines.append(make_line(mod_body(module, millivolts)))
    lines.append(CYCLE_END)
    return lines


# --- 5. send: every cycle_ms, never stopping the emulator (REQ-41) ------------
# Warnings go to stderr, so they do not mix with the status lines on stdout.


def send(result: PackResult) -> None:
    """Send every line if a cycle starts this tick. Reopen a lost port first."""
    if state is None:
        raise RuntimeError("uart_port.send() called before start()")

    if state.port is None:
        try_reopen()
    if state.port is not None and state.tick_count % state.every_ticks == 0:
        write_lines(pack_lines(result, state.settings.modules))

    state.tick_count += 1


def write_lines(lines: list[str]) -> None:
    """One write per line, so a write that times out drops one line, not a
    whole cycle. If one fails, the port is given up and the rest of this
    cycle is not sent."""
    for line in lines:
        try:
            state.port.write(line.encode("ascii"))
        except (serial.SerialException, OSError) as error:
            lose_port("writing to", error)
            return
    if state.failing:
        state.failing = False
        print("interfaces.uart: sending works again", file=sys.stderr)


def lose_port(action: str, error: Exception) -> None:
    """Close a port that failed. send() reopens it every 2 s.

    Warns once when it starts failing, not on every try. A line half read
    before the failure is thrown away.
    """
    if not state.failing:
        state.failing = True
        print(
            f"Warning: interfaces.uart: {action} {state.settings.port} failed "
            f"({error}). Lines are dropped and the port is reopened every 2 s "
            f"until it works again.",
            file=sys.stderr,
        )
    try:
        state.port.close()
    except (serial.SerialException, OSError):
        pass  # it is already gone, for example a USB adapter unplugged
    state.port = None
    state.lost_tick = state.tick_count
    state.buffer = b""
    state.skipping = False


def try_reopen() -> None:
    """Every REOPEN_TICKS ticks since the port was lost, try to open it again.
    "sending works again" is printed after the next good write."""
    if state.tick_count - state.lost_tick < REOPEN_TICKS:
        return
    state.lost_tick = state.tick_count  # the next try is 2 s from now
    try:
        state.port = open_port(state.settings)
    except serial.SerialException:
        return  # still gone, already warned
    print(f"interfaces.uart: {state.settings.port} open again", file=sys.stderr)


# --- 6. read_current: receiving $CUR lines (REQ-40) ---------------------------


def read_current() -> float | None:
    """The newest $CUR value in amps, or None if nothing new arrived.

    Reads every byte waiting without blocking. A line whose end has not
    arrived yet is kept and finished on a later call. If several $CUR lines
    arrived since the last call, only the newest counts. Bad lines are
    ignored with a warning, once per kind of problem.
    """
    if state is None:
        raise RuntimeError("uart_port.read_current() called before start()")
    if state.port is None:
        return None  # lost, send() is reopening it

    try:
        waiting = state.port.in_waiting
        data = state.port.read(waiting) if waiting else b""
    except (serial.SerialException, OSError) as error:
        lose_port("reading from", error)
        return None

    newest = None
    for line in complete_lines(data):
        value = command_value(line)
        if value is not None:
            newest = value
    return newest


def complete_lines(data: bytes) -> list[bytes]:
    """Add data to the buffer and take out every line that has its \\n.

    \\r is removed. If the unfinished part grows past MAX_LINE, it is thrown
    away and the rest of that line, up to its \\n, is skipped, so the buffer
    never grows without end.
    """
    *complete, state.buffer = (state.buffer + data).split(b"\n")
    lines = []
    for raw in complete:
        if state.skipping:
            state.skipping = False  # this was the end of a too long line
            continue
        lines.append(raw.rstrip(b"\r"))

    if len(state.buffer.rstrip(b"\r")) > MAX_LINE:
        warn_once("too long", f"ignored a line longer than {MAX_LINE} characters")
        state.buffer = b""
        state.skipping = True
    return lines


def command_value(line: bytes) -> float | None:
    """The amps of a good $CUR line, else None (with a warning if bad)."""
    if not line:
        return None  # an empty line, for example a lone \r\n
    if len(line) > MAX_LINE:
        warn_once("too long", f"ignored a line longer than {MAX_LINE} characters")
        return None
    fields = check_line(line)
    if fields is None:
        return None

    kind, values = fields[0], fields[1:]
    if kind in OWN_TYPES:
        return None  # our own lines coming back, for example on loopback wiring
    if kind == COMMAND_TYPE and len(values) == 1 and NUMBER.fullmatch(values[0]):
        return float(values[0])
    warn_once(
        "format",
        f"ignored a line with a wrong format: {line[:40]!r} "
        f"(expected $CUR,<amps>*CS, like $CUR,50*6D)",
    )
    return None


def check_line(line: bytes) -> list[str] | None:
    """The fields between $ and *, if the line has the right shape and
    checksum. Otherwise None, with a warning."""
    try:
        text = line.decode("ascii")
    except UnicodeDecodeError:
        text = ""  # not ASCII: reported as a wrong format below
    given = text[-2:]
    if (
        not text.startswith("$")
        or len(text) < 4
        or text[-3] != "*"
        or not all(character in string.hexdigits for character in given)
    ):
        warn_once(
            "format",
            f"ignored a line with a wrong format: {line[:40]!r} "
            f"(expected $TYPE,field,...*CS)",
        )
        return None

    body = text[1:-3]
    if int(given, 16) != checksum(body):
        warn_once(
            "checksum",
            f"ignored a line with a wrong checksum: {text[:40]!r} "
            f"(expected *{checksum(body):02X})",
        )
        return None
    return body.split(",")


def warn_once(key: str, text: str) -> None:
    """Print a warning the first time for this key, never again until restart."""
    if key not in state.warned:
        state.warned.add(key)
        print(f"Warning: interfaces.uart: {text} (warned once)", file=sys.stderr)


# --- 7. stop ------------------------------------------------------------------


def stop() -> None:
    """Close the port. Safe to call more than once."""
    global state
    if state is not None:
        if state.port is not None:
            state.port.close()
        state = None
