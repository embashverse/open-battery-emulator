"""Tests for interfaces/uart: lines sent, $CUR received, bad input, a failing port.

Every test uses pyserial's loop:// port, so no serial hardware is needed (CI has
none). On loop:// whatever is written can be read back from the same port: the
tests read the emulator's lines from uart_port.state.port, and write $CUR lines
into it for read_current() to find. The pack comes from the test battery in
conftest.py.
"""

import json
import urllib.request
from pathlib import Path

import can
import pytest
import serial
from conftest import FakeClock, write_config

from emulator.engine import run, start_engine, step, stop_engine
from emulator.pack import PackInfo, pack_info
from interfaces.uart import uart_port

ROOT = Path(__file__).parent.parent
DBC_PATH = ROOT / "interfaces" / "can" / "battery.dbc"


# --- Helpers ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def close_uart_port():
    """Every test starts and ends with the UART interface stopped."""
    uart_port.stop()
    yield
    uart_port.stop()


def uart_settings(**changes):
    """The interfaces.uart section, on the loop:// test port."""
    settings = {
        "enabled": True,
        "port": "loop://",
        "baudrate": 115200,
        "cycle_ms": 1000,
        "modules": False,
    }
    settings.update(changes)
    return settings


def make_engine(tmp_path, cells=3, series=2, parallel=2):
    """An engine without interfaces, for its pack and results."""
    path = write_config(
        tmp_path,
        {
            "pack.cells_per_module": cells,
            "pack.modules_in_series": series,
            "pack.strings_in_parallel": parallel,
            "variation.soc_pct": 2,  # every cell a little different
        },
    )
    return start_engine(path)


def uart_engine(tmp_path, source="uart", extra=None, **uart_changes):
    """An engine with the UART interface on loop://, started and RUNNING."""
    changes = {
        "current.source": source,
        "interfaces.uart": uart_settings(**uart_changes),
        **(extra or {}),
    }
    return start_engine(write_config(tmp_path, changes))


def write_to_port(data):
    """Put bytes (or text) on the port, as if a device had sent them."""
    if isinstance(data, str):
        data = data.encode("ascii")
    uart_port.state.port.write(data)


def sent_text():
    """Everything waiting on the loop:// port, as it was sent."""
    port = uart_port.state.port
    return port.read(port.in_waiting).decode("ascii")


def sent_lines():
    """Every non-empty line waiting on the port, each with its \\r\\n. Empty
    lines (the end of each cycle) are skipped, like a receiver does."""
    return [line for line in sent_text().splitlines(keepends=True) if line != "\r\n"]


def fields(line):
    """The fields of one sent line, after checking its shape and checksum."""
    assert line.startswith("$") and line.endswith("\r\n")
    body, given = line[1:-2].split("*")
    assert given == f"{uart_port.checksum(body):02X}"  # two upper case hex digits
    return body.split(",")


def info(cells=12, series=8, parallel=2, v_max=3.65, max_a=200.0):
    return PackInfo(
        cells_per_module=cells,
        modules_in_series=series,
        strings_in_parallel=parallel,
        v_max=v_max,
        max_charge_a=max_a,
        max_discharge_a=max_a,
    )


# --- Lines sent ---------------------------------------------------------------


def test_every_line_has_the_right_fields_and_a_valid_checksum(tmp_path):
    """Checks REQ-39: $PACK, $CELL and $LIM carry the pack values with the set
    decimals, and every line ends in a correct checksum and \\r\\n."""
    engine = make_engine(tmp_path)
    assert uart_port.start(uart_settings(), pack_info(engine.pack)) is True
    result = step(engine)
    uart_port.send(result)

    pack, cell, lim = (fields(line) for line in sent_lines())
    low, high = result.lowest_cell, result.highest_cell
    assert pack == [
        "PACK",
        f"{result.voltage_v:.2f}",
        f"{result.current_a:.1f}",
        f"{result.soc_pct:.2f}",
        "DISCHARGING",
        "0",
        "0",
    ]
    assert cell == [
        "CELL",
        f"{low.voltage_v:.3f}",
        str(low.module),
        str(low.cell),
        f"{high.voltage_v:.3f}",
        str(high.module),
        str(high.cell),
    ]
    assert lim == [
        "LIM",
        f"{result.max_charge_a:.1f}",
        f"{result.max_discharge_a:.1f}",
    ]


def test_checksum_is_the_xor_between_dollar_and_star():
    """Checks REQ-39: the checksum of the example line in
    docs/interfaces/uart.md."""
    assert uart_port.make_line("CUR,50") == "$CUR,50*6D\r\n"


@pytest.mark.parametrize("modules", [False, True])
def test_each_cycle_ends_with_exactly_one_empty_line(tmp_path, modules):
    """Checks REQ-39: after each cycle's lines comes one empty line and no other
    empty line; the lines themselves have no spaces."""
    engine = make_engine(tmp_path)
    uart_port.start(
        uart_settings(cycle_ms=100, modules=modules), pack_info(engine.pack)
    )
    result = step(engine)

    uart_port.send(result)
    first = sent_text()
    uart_port.send(result)
    second = sent_text()

    for cycle in (first, second):
        lines = cycle.splitlines(keepends=True)
        assert lines[-1] == "\r\n"  # the cycle ends with the empty line
        assert "\r\n" not in lines[:-1]  # and it is the only one
        assert len(lines) == 1 + (3 + 4 if modules else 3)  # 4 modules
        assert all(" " not in line for line in lines)
        for line in lines[:-1]:
            fields(line)  # every other line is a full, valid line


def test_lines_are_sent_every_cycle_ms(tmp_path):
    """Checks REQ-39: with cycle_ms 300, a full set of lines every 3rd tick
    and nothing in between."""
    engine = make_engine(tmp_path)
    uart_port.start(uart_settings(cycle_ms=300), pack_info(engine.pack))
    result = step(engine)

    counts = []
    for _ in range(7):
        uart_port.send(result)
        counts.append(len(sent_lines()))
    assert counts == [3, 0, 0, 3, 0, 0, 3]


def test_module_lines_only_when_modules_is_true(tmp_path):
    """Checks REQ-39: no $MOD lines with modules false; with true, one per
    module, numbered string by string, cells in whole mV."""
    engine = make_engine(tmp_path, cells=3, series=2, parallel=2)
    result = step(engine)

    uart_port.start(uart_settings(modules=False), pack_info(engine.pack))
    uart_port.send(result)
    assert [fields(line)[0] for line in sent_lines()] == ["PACK", "CELL", "LIM"]
    uart_port.stop()

    uart_port.start(uart_settings(modules=True), pack_info(engine.pack))
    uart_port.send(result)
    module_lines = [fields(line) for line in sent_lines()][3:]
    assert len(module_lines) == 4  # 2 in series x 2 strings
    for module, line in enumerate(module_lines, start=1):
        voltages = result.module_cell_voltages[module - 1]
        assert line == ["MOD", str(module), *(str(round(v * 1000)) for v in voltages)]


# --- Receiving $CUR -----------------------------------------------------------


def test_cur_line_changes_the_pack_current(tmp_path):
    """Checks REQ-40: with current.source uart, $CUR sets the pack current,
    discharge positive."""
    engine = uart_engine(tmp_path)

    write_to_port("$CUR,50*6D\r\n")
    result = step(engine)
    assert (result.current_a, result.current_timeout) == (pytest.approx(50), False)
    assert result.state == "DISCHARGING"

    write_to_port(uart_port.make_line("CUR,-20"))
    result = step(engine)
    assert result.current_a == pytest.approx(-20)
    assert result.state == "CHARGING"
    stop_engine(engine)


def test_bad_lines_are_ignored_with_one_warning_each(tmp_path, capsys):
    """Checks REQ-40: wrong checksum, wrong format and too long lines are
    ignored, each kind warned once, and good lines still work afterwards. The
    emulator's own line types coming back are ignored without a warning."""
    engine = uart_engine(tmp_path)
    bad = [
        "$CUR,50*00\r\n",  # wrong checksum
        "CUR,50\r\n",  # no $ and no checksum
        uart_port.make_line("CUR,abc"),  # not a number
        uart_port.make_line("XYZ,1"),  # unknown type
        uart_port.make_line("CUR," + "1" * 200),  # too long
        uart_port.make_line("PACK,1.00,0.0,50.00,IDLE,0,0"),  # own line, echoed
    ]
    for _ in range(2):
        for line in bad:
            write_to_port(line)
        result = step(engine)
        assert (result.current_a, result.current_timeout) == (0, True)

    err = capsys.readouterr().err
    assert err.count("wrong checksum") == 1
    assert err.count("wrong format") == 1
    assert err.count("longer than 128 characters") == 1
    assert "PACK" not in err

    write_to_port("$CUR,50*6D\r\n")
    assert step(engine).current_a == pytest.approx(50)
    stop_engine(engine)


def test_line_split_across_two_reads_is_put_back_together(tmp_path):
    """Checks REQ-40: half a line waits for its end, then counts."""
    engine = uart_engine(tmp_path)
    line = uart_port.make_line("CUR,40")

    write_to_port(line[:5])
    assert step(engine).current_timeout is True  # nothing complete yet
    write_to_port(line[5:])
    result = step(engine)
    assert (result.current_a, result.current_timeout) == (pytest.approx(40), False)
    stop_engine(engine)


def test_too_long_line_in_pieces_is_dropped_and_the_next_works(tmp_path, capsys):
    """Checks REQ-40: an unfinished line past 128 characters is thrown away
    (the buffer never grows), its rest is skipped, the next line counts."""
    engine = uart_engine(tmp_path)

    write_to_port("$CUR," + "1" * 150)  # no end yet, already too long
    step(engine)
    assert uart_port.state.buffer == b""
    write_to_port("1111*00\r\n" + uart_port.make_line("CUR,30"))
    result = step(engine)

    assert result.current_a == pytest.approx(30)
    err = capsys.readouterr().err
    assert err.count("longer than 128 characters") == 1
    assert "wrong checksum" not in err  # the rest of the long line was skipped
    stop_engine(engine)


def test_timeout_with_uart_as_the_source(tmp_path):
    """Checks REQ-10, REQ-40: in timeout until the first $CUR, held for
    timeout_ms, then 0 A and the flag; a new $CUR clears it at once."""
    engine = uart_engine(tmp_path)
    start = step(engine)
    assert (start.current_a, start.current_timeout) == (0, True)

    write_to_port(uart_port.make_line("CUR,30"))
    assert step(engine).current_a == pytest.approx(30)
    for _ in range(5):  # 100 to 500 ms since the command: still held
        result = step(engine)
        assert (result.current_a, result.current_timeout) == (
            pytest.approx(30),
            False,
        )

    late = step(engine)  # 600 ms since the command
    assert (late.current_a, late.current_timeout) == (0, True)

    write_to_port(uart_port.make_line("CUR,20"))
    back = step(engine)
    assert (back.current_a, back.current_timeout) == (pytest.approx(20), False)
    stop_engine(engine)


# --- With the engine, CAN and the web UI --------------------------------------


def test_can_and_uart_send_at_the_same_time(tmp_path):
    """Checks REQ-14, REQ-39: CAN (virtual bus) and UART (loop://) enabled
    together both send while the loop runs."""
    channel = f"test-{tmp_path.name}"
    receiver = can.Bus(interface="virtual", channel=channel)
    can_section = {
        "enabled": True,
        "driver": "virtual",
        "channel": channel,
        "bitrate": 500000,
        "dbc": str(DBC_PATH),
        "module_frames": {
            "base_id": 0x200,
            "id_step": 0x10,
            "cells_per_frame": 4,
            "cycle_ms": 1000,
        },
    }
    engine = uart_engine(
        tmp_path, source="simulated", extra={"interfaces.can": can_section}
    )
    try:
        clock = FakeClock()
        run(engine, max_ticks=10, clock=clock.clock, sleep=clock.sleep, output=print)

        frame_ids = []
        while (frame := receiver.recv(timeout=0)) is not None:
            frame_ids.append(frame.arbitration_id)
        assert frame_ids.count(0x100) == 10  # every tick
        assert [fields(line)[0] for line in sent_lines()] == ["PACK", "CELL", "LIM"]
    finally:
        stop_engine(engine)
        receiver.shutdown()


def test_uart_shows_in_the_web_ui_without_webui_code(tmp_path):
    """Checks REQ-18, REQ-38: uart is in the live state's source options and in
    the Config form fields, and can be picked as the source, while webui/ has
    no code that names it."""
    engine = uart_engine(
        tmp_path,
        source="simulated",
        extra={"webui": {"enabled": True, "host": "127.0.0.1", "port": 0}},
    )
    base = f"http://127.0.0.1:{engine.webui_port}"
    try:
        step(engine)
        with urllib.request.urlopen(base + "/api/state", timeout=5) as response:
            state = json.load(response)
        assert {"name": "uart", "available": True, "reason": None} in state[
            "source_options"
        ]
        assert "uart" in state["sources"]

        with urllib.request.urlopen(base + "/api/config", timeout=5) as response:
            form = json.load(response)
        by_path = {field["path"]: field for field in form["fields"]}
        assert by_path["interfaces.uart.port"]["value"] == "loop://"
        assert by_path["interfaces.uart.baudrate"]["type"] == "int"
        assert by_path["interfaces.uart.modules"]["type"] == "bool"

        request = urllib.request.Request(
            base + "/api/command",
            data=json.dumps({"command": "set_source", "value": "uart"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(request, timeout=5).close()
        step(engine)
        assert engine.current_source == "uart"
    finally:
        stop_engine(engine)

    for path in (ROOT / "webui").rglob("*"):
        if path.suffix in (".py", ".html", ".js", ".css"):
            assert "uart" not in path.read_text().lower(), path


# --- Start checks -------------------------------------------------------------


def test_port_that_does_not_exist_names_the_setting(tmp_path):
    """Checks REQ-38: a port that cannot be opened at start is an error naming
    interfaces.uart.port, also through the engine."""
    with pytest.raises(uart_port.UartSettingsError, match=r"interfaces\.uart\.port"):
        uart_port.start(uart_settings(port="/dev/does-not-exist"), info())
    assert uart_port.state is None

    with pytest.raises(ValueError, match=r"interfaces\.uart\.port.*does-not-exist"):
        uart_engine(tmp_path, port="/dev/does-not-exist")


@pytest.mark.parametrize(
    "changes, message",
    [
        ({"cycle_ms": 150}, r"interfaces\.uart\.cycle_ms"),
        ({"cycle_ms": 0}, r"interfaces\.uart\.cycle_ms"),
        ({"baudrate": 0}, r"interfaces\.uart\.baudrate"),
        ({"baudrate": "fast"}, r"interfaces\.uart\.baudrate"),
        ({"modules": "yes"}, r"interfaces\.uart\.modules"),
        ({"port": ""}, r"interfaces\.uart\.port"),
    ],
)
def test_bad_setting_names_the_setting(changes, message):
    """Checks REQ-38: every bad uart setting is an error naming it."""
    with pytest.raises(uart_port.UartSettingsError, match=message):
        uart_port.start(uart_settings(**changes), info(cells=1, series=1, parallel=1))


def test_missing_settings_use_their_defaults():
    """Checks REQ-38: baudrate 115200, cycle_ms 1000 and modules false when
    missing."""
    checked = uart_port.check_settings({"enabled": True, "port": "loop://"})
    assert (checked.baudrate, checked.cycle_ms, checked.modules) == (
        115200,
        1000,
        False,
    )
    assert uart_port.check_settings({"enabled": True}).port == "/dev/serial0"


def test_too_slow_baudrate_for_the_lines_is_refused(tmp_path):
    """Checks REQ-42: 96s2p with module lines at 9600 baud needs more than 80 %
    of the line, so start names interfaces.uart.baudrate; without module lines
    it fits. The estimate is never below what one cycle really sends."""
    with pytest.raises(
        uart_port.UartSettingsError, match=r"interfaces\.uart\.baudrate: 9600 baud"
    ):
        uart_port.start(uart_settings(baudrate=9600, modules=True), info())
    assert uart_port.start(uart_settings(baudrate=9600, modules=False), info())
    uart_port.stop()

    engine = make_engine(tmp_path, cells=12, series=8, parallel=2)
    settings = uart_port.check_settings(uart_settings(modules=True))
    uart_port.start(uart_settings(modules=True), pack_info(engine.pack))
    uart_port.send(step(engine))
    sent_bytes = len(sent_text())  # the empty line that ends the cycle too
    assert sent_bytes <= uart_port.bytes_per_cycle(settings, pack_info(engine.pack))


# --- Sending never stops the emulator -----------------------------------------


def broken_write(data):
    raise serial.SerialException("write failed: [Errno 5] Input/output error")


def test_write_error_does_not_stop_the_emulator(tmp_path, capsys, monkeypatch):
    """Checks REQ-41: when writing fails the loop keeps running, it is warned
    once, the port is reopened after 2 s and sending works again."""
    engine = uart_engine(tmp_path, source="simulated", cycle_ms=100)
    monkeypatch.setattr(uart_port.state.port, "write", broken_write)
    try:
        clock = FakeClock()
        run(engine, max_ticks=30, clock=clock.clock, sleep=clock.sleep, output=print)

        assert engine.tick_count == 30
        err = capsys.readouterr().err
        assert err.count("Warning: interfaces.uart") == 1
        assert "open again" in err
        assert "sending works again" in err
        assert len(sent_lines()) == 3 * 10  # ticks 20 to 29 on the reopened port
    finally:
        stop_engine(engine)


def test_lost_port_is_reopened_every_2_seconds(tmp_path, monkeypatch):
    """Checks REQ-41: after the port is lost, reopening is tried every 20 ticks
    (2 s) until it works; then lines go out again."""
    engine = make_engine(tmp_path)
    uart_port.start(uart_settings(cycle_ms=100), pack_info(engine.pack))
    result = step(engine)
    monkeypatch.setattr(uart_port.state.port, "write", broken_write)

    tries = []
    real_open = uart_port.open_port

    def unplugged_twice(settings):
        tries.append(uart_port.state.tick_count)
        if len(tries) <= 2:
            raise serial.SerialException("could not open port: no such file")
        return real_open(settings)

    monkeypatch.setattr(uart_port, "open_port", unplugged_twice)
    for _ in range(61):  # ticks 0 to 60
        uart_port.send(result)
        if uart_port.state.port is None:
            assert uart_port.read_current() is None  # lost: nothing to read

    assert tries == [20, 40, 60]
    assert [fields(line)[0] for line in sent_lines()] == ["PACK", "CELL", "LIM"]
