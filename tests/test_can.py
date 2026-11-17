"""Tests for interfaces/can: DBC messages, module frames, cycle times, start checks.

Every test uses python-can's virtual bus, so no vcan0 is needed (CI has none).
A second "receiver" bus on the same virtual channel sees every frame sent.
The pack comes from the test battery in conftest.py.
"""

import math
import re
import shutil
import struct
from pathlib import Path

import can
import cantools
import pytest
from conftest import FakeClock, write_config

from emulator.cell import cell_voltage
from emulator.engine import run, start_engine, step, stop_interfaces
from emulator.pack import PackInfo, pack_info
from interfaces.can import can_bus

DBC_PATH = Path(__file__).parent.parent / "interfaces" / "can" / "battery.dbc"
DATABASE = cantools.database.load_file(DBC_PATH)


# --- Helpers ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def close_can_bus():
    """Every test starts and ends with the CAN interface stopped."""
    can_bus.stop()
    yield
    can_bus.stop()


@pytest.fixture
def channel(tmp_path):
    """A virtual channel name used only by this test."""
    return f"test-{tmp_path.name}"


@pytest.fixture
def receiver(channel):
    bus = can.Bus(interface="virtual", channel=channel)
    yield bus
    bus.shutdown()


def can_settings(channel, dbc=DBC_PATH, **module_frames):
    """The interfaces.can section, with the virtual driver and default pattern."""
    frames = {"base_id": 0x200, "id_step": 0x10, "cells_per_frame": 4, "cycle_ms": 1000}
    frames.update(module_frames)
    return {
        "enabled": True,
        "driver": "virtual",
        "channel": channel,
        "bitrate": 500000,
        "dbc": str(dbc),
        "module_frames": frames,
    }


def make_engine(tmp_path, cells=12, series=8, parallel=2, **changes):
    """An engine without interfaces, for its pack and results."""
    path = write_config(
        tmp_path,
        {
            "pack.cells_per_module": cells,
            "pack.modules_in_series": series,
            "pack.strings_in_parallel": parallel,
            "variation.soc_pct": 2,  # every cell a little different
            "variation.r0_pct": 5,
            **changes,
        },
    )
    return start_engine(path)


def received(bus):
    """Every frame waiting on the receiver bus, in order."""
    frames = []
    while (frame := bus.recv(timeout=0)) is not None:
        frames.append(frame)
    return frames


def info(cells=12, series=8, parallel=2, v_max=3.65, max_a=200.0):
    return PackInfo(
        cells_per_module=cells,
        modules_in_series=series,
        strings_in_parallel=parallel,
        v_max=v_max,
        max_charge_a=max_a,
        max_discharge_a=max_a,
    )


# --- DBC messages -------------------------------------------------------------


def test_pack_messages_decode_to_pack_values(tmp_path, channel, receiver):
    """Checks REQ-12: 0x100 to 0x102 decode with the DBC to the pack values."""
    engine = make_engine(tmp_path, cells=3, series=2, parallel=2)
    can_bus.start(can_settings(channel), pack_info(engine.pack))
    result = step(engine)
    can_bus.send(result)

    by_id = {frame.arbitration_id: frame for frame in received(receiver)}
    status = DATABASE.decode_message(0x100, by_id[0x100].data)
    cells = DATABASE.decode_message(0x101, by_id[0x101].data)
    limits = DATABASE.decode_message(0x102, by_id[0x102].data)

    # Within one scaling step: 0.1 V, 0.1 A, 0.1 %, 1 mV.
    assert status["PackVoltage"] == pytest.approx(result.voltage_v, abs=0.1)
    assert status["PackCurrent"] == pytest.approx(result.current_a, abs=0.1)
    assert status["PackSOC"] == pytest.approx(result.soc_pct, abs=0.1)
    assert status["PackState"] == result.state == "DISCHARGING"
    assert status["CurrentTimeout"] == 0
    assert status["TempOutOfRange"] == 0
    assert status["AliveCounter"] == 0

    high, low = result.highest_cell, result.lowest_cell
    assert cells["CellVmax"] == pytest.approx(high.voltage_v, abs=0.001)
    assert cells["CellVmaxModule"] == high.module
    assert cells["CellVmaxCell"] == high.cell
    assert cells["CellVmin"] == pytest.approx(low.voltage_v, abs=0.001)
    assert cells["CellVminModule"] == low.module
    assert cells["CellVminCell"] == low.cell

    assert limits["MaxChargeCurrent"] == pytest.approx(result.max_charge_a, abs=0.1)
    assert limits["MaxDischargeCurrent"] == pytest.approx(
        result.max_discharge_a, abs=0.1
    )


def test_each_message_appears_at_its_cycle_time(tmp_path, channel, receiver):
    """Checks REQ-12, REQ-13: 0x100 to 0x102 every tick, module frames every 10th."""
    engine = make_engine(tmp_path, cells=12, series=2, parallel=1)
    can_bus.start(can_settings(channel), pack_info(engine.pack))

    ids_per_tick = []
    for _ in range(20):
        can_bus.send(step(engine))
        ids_per_tick.append([frame.arbitration_id for frame in received(receiver)])

    for tick, ids in enumerate(ids_per_tick):
        assert ids[:3] == [0x100, 0x101, 0x102]
        module_ids = ids[3:]
        if tick % 10 == 0:
            assert module_ids == [
                0x200,
                0x201,
                0x202,
                0x20F,
                0x210,
                0x211,
                0x212,
                0x21F,
            ]
        else:
            assert module_ids == []


def test_alive_counter_wraps_from_255_to_0(tmp_path, channel, receiver):
    """Checks REQ-12: AliveCounter counts 0 to 255, then starts at 0 again."""
    engine = make_engine(tmp_path, cells=1, series=1, parallel=1)
    can_bus.start(can_settings(channel), pack_info(engine.pack))
    result = step(engine)

    counters = []
    for _ in range(258):
        can_bus.send(result)
        for frame in received(receiver):
            if frame.arbitration_id == 0x100:
                decoded = DATABASE.decode_message(0x100, frame.data)
                counters.append(decoded["AliveCounter"])

    assert counters == list(range(256)) + [0, 1]


# --- Module frames ------------------------------------------------------------


def test_module_frames_follow_the_pattern(tmp_path, channel, receiver):
    """Checks REQ-13: 12 cells, 8 series, 2 parallel: IDs, cell order, module 9."""
    engine = make_engine(tmp_path, cells=12, series=8, parallel=2)
    can_bus.start(can_settings(channel), pack_info(engine.pack))
    result = step(engine)
    can_bus.send(result)
    frames = {
        frame.arbitration_id: frame
        for frame in received(receiver)
        if frame.arbitration_id >= 0x200
    }

    # 16 modules x (3 cell frames + 1 status frame), all 8 bytes long.
    expected_ids = []
    for module in range(1, 17):
        first = 0x200 + (module - 1) * 0x10
        expected_ids += [first, first + 1, first + 2, first + 0xF]
    assert sorted(frames) == expected_ids
    assert all(frame.dlc == 8 for frame in frames.values())

    # Cell order: frame n of module m holds cells 4n+1 to 4n+4, in mV.
    for module, voltages in enumerate(result.module_cell_voltages, start=1):
        first = 0x200 + (module - 1) * 0x10
        sent = []
        for n in range(3):
            sent += struct.unpack("<4H", frames[first + n].data)
        assert sent == [round(v * 1000) for v in voltages]

    # Module 9 (0x280) is the first module of string 2: worked out here from the
    # pack's cells, not from the result.
    string_2_module_1 = engine.pack.strings[1].modules[0]
    string_2_current = result.strings[1].current_a
    expected_mv = [
        round(1000 * cell_voltage(cell, engine.pack.battery, string_2_current, 25))
        for cell in string_2_module_1.cells
    ]
    sent = []
    for n in range(3):
        sent += struct.unpack("<4H", frames[0x280 + n].data)
    assert sent == expected_mv

    # Status frame: temperature 25.0 degC as 250, flags 0, the rest 0xFF.
    status = frames[0x28F].data
    assert struct.unpack("<hB", status[:3]) == (250, 0)
    assert status[3:] == bytes([0xFF] * 5)


def test_short_last_frame_is_filled_with_0xff(tmp_path, channel, receiver):
    """Checks REQ-13: 10 cells at 4 per frame: the last frame holds 2 cells + 0xFF."""
    engine = make_engine(tmp_path, cells=10, series=1, parallel=1)
    can_bus.start(can_settings(channel), pack_info(engine.pack))
    result = step(engine)
    can_bus.send(result)
    frames = {f.arbitration_id: f for f in received(receiver)}

    last = frames[0x202]
    assert last.dlc == 8
    voltages = result.module_cell_voltages[0]
    assert list(struct.unpack("<2H", last.data[:4])) == [
        round(v * 1000) for v in voltages[8:10]
    ]
    assert last.data[4:] == bytes([0xFF] * 4)
    assert 0x203 not in frames


# --- Start checks -------------------------------------------------------------


def test_unknown_dbc_signal_name_is_a_clear_error(tmp_path, channel):
    """Checks REQ-12: a DBC signal the emulator does not know stops the start."""
    dbc = tmp_path / "renamed.dbc"
    dbc.write_text(DBC_PATH.read_text().replace("PackSOC", "PackSoc"))

    message = "signal PackSoc in message PackStatus (0x100) is not a name"
    with pytest.raises(ValueError, match=re.escape(message)):
        can_bus.start(can_settings(channel, dbc=dbc), info())
    assert can_bus.state is None  # no bus opened


@pytest.mark.parametrize(
    ("pack", "frames", "message"),
    [
        (info(), {"id_step": 0x08}, "id_step 0x8 must be at least 0x10"),
        (
            info(cells=16),
            {"cells_per_frame": 1},
            "16 cells at 1 per frame need 16 frames, but only 15 fit",
        ),
        (info(), {"base_id": 0x100}, "the ID of DBC message PackStatus"),
        (info(), {"base_id": 0x710}, "IDs up to 0x80F, above the highest"),
        (info(), {"cells_per_frame": 5}, "cells_per_frame must be at most 4"),
        (info(), {"cycle_ms": 150}, "cycle_ms must be a multiple of 100"),
    ],
)
def test_overlapping_module_ids_are_a_clear_error(channel, pack, frames, message):
    """Checks REQ-13: module frames that overlap other IDs stop the start."""
    with pytest.raises(ValueError, match=re.escape(message)):
        can_bus.start(can_settings(channel, **frames), pack)
    assert can_bus.state is None


def test_value_outside_a_signal_range_is_a_clear_error(channel):
    """Checks REQ-12: a pack whose voltage cannot fit PackVoltage stops the start."""
    big_pack = info(cells=200, series=10, parallel=1)  # 2000 x 3.65 V = 7300 V
    message = "signal PackVoltage in message PackStatus (0x100) covers 0 to 6553.5"
    with pytest.raises(ValueError, match=re.escape(message)):
        can_bus.start(can_settings(channel), big_pack)


def test_module_number_must_fit_its_signal(channel):
    """Checks REQ-12: more modules than CellVmaxModule can hold stops the start."""
    many_modules = info(cells=1, series=128, parallel=2)  # 256 modules, u8 holds 255
    with pytest.raises(ValueError, match="signal CellVmaxModule"):
        can_bus.start(can_settings(channel, id_step=0x10, base_id=0), many_modules)


# --- Through the engine -------------------------------------------------------


def test_engine_sends_on_can_every_tick_and_stops(tmp_path, channel, receiver):
    """Checks REQ-11, REQ-14: the engine starts can from config.yaml, sends, stops.

    The DBC path is relative to config.yaml, like in the real config.
    """
    shutil.copy(DBC_PATH, tmp_path / "battery.dbc")
    settings = can_settings(channel, dbc="battery.dbc")
    path = write_config(
        tmp_path,
        {"pack.cells_per_module": 4, "interfaces.can": settings},
    )

    engine = start_engine(path)
    fake = FakeClock()
    run(engine, max_ticks=10, clock=fake.clock, sleep=fake.sleep, output=print)
    stop_interfaces(engine)

    ids = [frame.arbitration_id for frame in received(receiver)]
    assert ids.count(0x100) == 10
    assert ids.count(0x200) == 1
    assert can_bus.state is None  # stop() closed the bus


# --- Receiving the current command (0x180) ------------------------------------
# The engine reads from can because current.source is "can". The receiver
# fixture is a second bus on the same virtual channel: it plays the device that
# sends the command, and it sees what the emulator sends. Timeout 500 ms = 5 ticks.


def command_engine(tmp_path, channel, **changes):
    """One cell, SOC 50, current from CAN on a virtual channel."""
    path = write_config(
        tmp_path,
        {
            "current.source": "can",
            "interfaces.can": can_settings(channel),
            **changes,
        },
    )
    return start_engine(path)


def send_command(bus, amps):
    """Send one CurrentCommand frame like a device would."""
    data = DATABASE.encode_message(0x180, {"CurrentCommand": amps})
    bus.send(can.Message(arbitration_id=0x180, is_extended_id=False, data=data))


def sent_status(bus):
    """The last 0x100 frame on the bus, decoded."""
    frames = [f for f in received(bus) if f.arbitration_id == 0x100]
    return DATABASE.decode_message(0x100, frames[-1].data)


def test_command_frame_changes_pack_current_and_soc(tmp_path, channel, receiver):
    """Checks REQ-08: a 0x180 frame from another device sets the pack current."""
    engine = command_engine(tmp_path, channel)
    send_command(receiver, 30)

    result = step(engine)

    assert result.current_a == pytest.approx(30)
    assert result.state == "DISCHARGING"
    assert not result.current_timeout
    # 30 A for 0.1 s on 100 Ah takes 30 * 0.1 / 3600 % out.
    assert result.soc_pct == pytest.approx(50 - 30 * 0.1 / 3600)


def test_newest_of_several_frames_is_used(tmp_path, channel, receiver):
    """Checks REQ-08: three commands in one tick: only the newest counts."""
    engine = command_engine(tmp_path, channel)
    for amps in (10, 20, -30):
        send_command(receiver, amps)

    assert step(engine).current_a == pytest.approx(-30)
    assert can_bus.read_current() is None  # nothing new since the last call


def test_start_is_in_timeout_until_the_first_command(tmp_path, channel, receiver):
    """Checks REQ-10: before any frame the current is 0 A and the flag is set."""
    engine = command_engine(tmp_path, channel)

    result = step(engine)

    assert result.current_a == 0
    assert result.current_timeout
    assert result.state == "IDLE"
    assert result.soc_pct == 50


def test_timeout_after_timeout_ms_and_cleared_by_a_new_frame(
    tmp_path, channel, receiver
):
    """Checks REQ-10: a command is held for 500 ms, then 0 A and the flag; a new
    frame clears it at once."""
    engine = command_engine(tmp_path, channel)
    send_command(receiver, 30)
    first = step(engine)
    assert (first.current_a, first.current_timeout) == (pytest.approx(30), False)

    for _ in range(5):  # 100 to 500 ms since the command: still held
        result = step(engine)
        assert (result.current_a, result.current_timeout) == (
            pytest.approx(30),
            False,
        )

    late = step(engine)  # 600 ms since the command
    assert (late.current_a, late.current_timeout) == (0, True)

    send_command(receiver, 20)
    back = step(engine)
    assert (back.current_a, back.current_timeout) == (pytest.approx(20), False)


def test_current_timeout_signal_matches_the_flag(tmp_path, channel, receiver):
    """Checks REQ-10, REQ-12: CurrentTimeout in 0x100 is 1 in timeout, else 0."""
    engine = command_engine(tmp_path, channel)

    can_bus.send(step(engine))
    assert sent_status(receiver)["CurrentTimeout"] == 1

    send_command(receiver, 30)
    can_bus.send(step(engine))
    status = sent_status(receiver)
    assert status["CurrentTimeout"] == 0
    assert status["PackCurrent"] == pytest.approx(30)


def test_received_current_inside_deadband_changes_nothing(tmp_path, channel, receiver):
    """Checks REQ-24: a received 0.3 A with a 0.5 A deadband counts as 0 A."""
    engine = command_engine(tmp_path, channel)

    for _ in range(100):
        send_command(receiver, 0.3)
        result = step(engine)

    assert result.current_a == 0
    assert result.soc_pct == 50
    assert result.state == "IDLE"
    assert not result.current_timeout  # commands did arrive


def test_dbc_without_current_command_cannot_be_the_source(tmp_path, channel):
    """Checks REQ-08, REQ-14: without a CurrentCommand signal can.start() returns
    False, so current.source: can stops the start and the bus is closed again."""
    dbc = tmp_path / "no_input.dbc"
    lines = DBC_PATH.read_text().splitlines()
    dbc.write_text(
        "\n".join(
            line
            for line in lines
            if "384" not in line and "SG_ CurrentCommand" not in line
        )
    )
    assert can_bus.start(can_settings(channel), info()) is True  # default DBC
    can_bus.stop()
    assert can_bus.start(can_settings(channel, dbc=dbc), info()) is False
    can_bus.stop()

    message = "current.source 'can' cannot provide current: interfaces.can has no"
    with pytest.raises(ValueError, match=re.escape(message)):
        command_engine(tmp_path, channel, **{"interfaces.can.dbc": str(dbc)})
    assert can_bus.state is None


def test_undecodable_command_frame_is_skipped(tmp_path, channel, receiver, capsys):
    """Checks REQ-27: a 0x180 frame that is too short is skipped with one warning;
    the last good command stays."""
    engine = command_engine(tmp_path, channel)
    send_command(receiver, 30)
    step(engine)

    for _ in range(3):
        receiver.send(
            can.Message(arbitration_id=0x180, is_extended_id=False, data=[1, 2])
        )
        result = step(engine)

    assert result.current_a == pytest.approx(30)
    assert capsys.readouterr().err.count("does not decode") == 1


# --- Sending never stops the emulator -----------------------------------------


def test_value_outside_signal_range_is_clamped_and_warned_once(
    tmp_path, channel, receiver, capsys
):
    """Checks REQ-27: 150 A with PackCurrent limited to +-100 A is sent as 100 A,
    warned once, and the emulator keeps running."""
    dbc = tmp_path / "narrow.dbc"
    dbc.write_text(
        DBC_PATH.read_text().replace(
            "PackCurrent : 16|16@1- (0.1,0) [-3276.8|3276.7]",
            "PackCurrent : 16|16@1- (0.1,0) [-100|100]",
        )
    )
    path = write_config(
        tmp_path,
        {
            "current.simulated_a": 150,
            "interfaces.can": can_settings(channel, dbc=dbc),
        },
    )
    engine = start_engine(path)
    fake = FakeClock()

    run(engine, max_ticks=20, clock=fake.clock, sleep=fake.sleep, output=print)

    assert engine.tick_count == 20
    assert engine.last_result.current_a == 150  # the pack itself is not limited
    assert sent_status(receiver)["PackCurrent"] == pytest.approx(100)
    warnings = capsys.readouterr().err
    assert warnings.count("PackCurrent 150 is outside -100 to 100, sent as 100") == 1


def test_module_frame_value_outside_its_range_is_clamped(
    tmp_path, channel, receiver, capsys
):
    """Checks REQ-27: a module temperature too big for int16 0.1 degC is clamped."""
    engine = make_engine(tmp_path, cells=1, series=1, parallel=1)
    engine.pack.temperature_c = 4000  # 40000 in 0.1 degC, above 32767
    can_bus.start(can_settings(channel), pack_info(engine.pack))

    can_bus.send(step(engine))

    status = {f.arbitration_id: f for f in received(receiver)}[0x20F]
    assert struct.unpack("<h", status.data[:2]) == (32767,)
    assert "module temperature (0.1 degC) 40000 is outside" in capsys.readouterr().err


def test_bus_refusing_frames_does_not_stop_the_emulator(
    tmp_path, channel, receiver, capsys, monkeypatch
):
    """Checks REQ-28: when the bus refuses frames, send() keeps going, warns once,
    and says so when sending works again."""
    engine = make_engine(tmp_path, cells=1, series=1, parallel=1)
    can_bus.start(can_settings(channel), pack_info(engine.pack))
    result = step(engine)

    def refuse(message, timeout=None):
        raise can.CanOperationError("No buffer space available")

    working_send = can_bus.state.bus.send
    monkeypatch.setattr(can_bus.state.bus, "send", refuse)
    for _ in range(5):
        can_bus.send(result)
    assert capsys.readouterr().err.count("the bus refused a frame") == 1

    monkeypatch.setattr(can_bus.state.bus, "send", working_send)
    can_bus.send(result)
    assert "sending works again" in capsys.readouterr().err
    assert 0x100 in [f.arbitration_id for f in received(receiver)]


def test_frames_per_module_count_matches_pattern():
    """Checks REQ-13: frames per module is cells / cells_per_frame, rounded up."""
    assert can_bus.module_frame_ids(0x200, 0x10, 2, math.ceil(12 / 4)) == [
        0x210,
        0x211,
        0x212,
        0x21F,
    ]
