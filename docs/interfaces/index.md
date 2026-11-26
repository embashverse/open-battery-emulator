# Interfaces

An interface is a way data leaves and enters the emulator: CAN or UART today. This page
explains where the current comes from; the pages below cover each interface.

## Where the current comes from

`current.source` in `config.yaml` (or the Current source control in the web UI) picks it:

| Source | Who sets the current | Device needed | Timeout |
|---|---|---|---|
| `simulated` | the emulator itself, from the web UI or `current.simulated_a` | no | no |
| `can` | a device on the CAN bus, frame 0x180 | yes | yes |
| `uart` | a device on the serial port, `$CUR` lines | yes | yes |

**Simulated** means the emulator sets the current itself, so it runs with nothing else
connected. With `can` or `uart`, a device sends the current instead, and the **timeout**
applies: if no command arrives within `current.timeout_ms` (500 ms by default), the
current is 0 A and the Timeout flag is set until the next command. At Play no command has
arrived yet, so it starts in timeout.

Sending pack data and receiving the current are separate: an enabled interface always
sends, whatever the source is. CAN and UART are both off by default.

| Page | For |
|---|---|
| [CAN](can.md) | messages, signals, the DBC file, module frames, sending current |
| [UART](uart.md) | line format, checksum, examples, `$CUR` input |
| [Testing without hardware](testing-without-hardware.md) | virtual CAN and a virtual UART, with the exact commands |
