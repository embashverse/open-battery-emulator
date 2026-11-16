import emulator.cell
import emulator.engine
import emulator.pack


def test_emulator_modules_import():
    """Checks the project skeleton (M0)."""
    assert emulator.cell.__doc__
    assert emulator.pack.__doc__
    assert emulator.engine.__doc__
