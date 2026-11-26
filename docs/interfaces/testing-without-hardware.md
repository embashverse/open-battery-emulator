# Testing without hardware

The exact commands to try CAN and UART with nothing plugged in: a virtual CAN device
(`vcan0`) and a virtual serial pair (`socat`). The emulator cannot tell the difference.

You need neither for the pack itself: with `current.source: simulated` the emulator sets
the current itself, with no device (see [Interfaces](index.md)).

## Virtual CAN

1. In `config.yaml` (or the Config tab):

    ```yaml
    interfaces:
      can:
        enabled: true
        channel: vcan0
    ```

2. Bring `vcan0` up:

    === "Raspberry Pi (service)"

        The service brings the bus up by itself at every boot when CAN is enabled. After
        changing `enabled`, `channel` or `bitrate`, restart once (or reboot):

        ```
        sudo systemctl restart open-battery-emulator-can open-battery-emulator
        ```

    === "Laptop"

        Create it after every reboot:

        ```
        sudo modprobe vcan
        sudo ip link add dev vcan0 type vcan
        sudo ip link set up vcan0
        ```

        Or the same from `config.yaml`: `sudo .venv/bin/python -m interfaces.can.bring_up`.
        Then start the emulator: `uv run python -m emulator.engine`.

3. Watch the frames (`candump` is in can-utils: `sudo apt install can-utils`). You see
   0x100 to 0x102 ten times a second and module frames from 0x200 once a second:

    ```
    candump vcan0
    candump vcan0 | uv run python -m cantools decode interfaces/can/battery.dbc
    ```

4. Send a current: set `current.source: can`, then repeat the 0x180 frame
   ([encoding](can.md#sending-current)):

    ```
    while true; do cansend vcan0 180#F401000000000000; sleep 0.1; done   # 50 A discharge
    while true; do cansend vcan0 180#38FF000000000000; sleep 0.1; done   # 20 A charge
    ```

    Stop the loop with Ctrl+C: within 0.5 s the status line shows `TIMEOUT` and 0 A.

## Virtual UART

1. Install the tools and start a virtual pair. Keep `socat` running in its own terminal;
   what goes into `/tmp/ttyOBE0` comes out of `/tmp/ttyOBE1` and back:

    ```
    sudo apt install socat picocom
    socat -d -d pty,raw,echo=0,link=/tmp/ttyOBE0 pty,raw,echo=0,link=/tmp/ttyOBE1
    ```

2. In `config.yaml`:

    ```yaml
    interfaces:
      uart:
        enabled: true
        port: /tmp/ttyOBE0
    ```

3. Start the emulator, and watch the other end (quit picocom with Ctrl+A, then Ctrl+X):

    ```
    picocom -b 115200 /tmp/ttyOBE1
    ```

    ```
    $PACK,315.36,50.0,80.00,DISCHARGING,0,0*79
    $CELL,3.283,7,5,3.287,16,6*31
    $LIM,200.0,200.0*48
    ```

4. Send a current: set `current.source: uart`, then in another terminal:

    ```
    while true; do printf '$CUR,50*6D\r\n' > /tmp/ttyOBE1; sleep 0.1; done    # 50 A discharge
    while true; do printf '$CUR,-20*47\r\n' > /tmp/ttyOBE1; sleep 0.1; done   # 20 A charge
    ```

`socat` must be started again after it stops or after a reboot. For a real adapter or the
Pi's own UART see [Hardware](../hardware.md). Problems: [Troubleshooting](../troubleshooting.md).
