# Hardware

How to connect real devices: the Pi's UART to a USB serial adapter, and a CAN HAT. You need
neither to try the emulator ([Testing without hardware](interfaces/testing-without-hardware.md)).

## Pi UART to a USB serial adapter (CP2102)

Connects the Pi's own UART to a laptop through a 3.3 V USB serial adapter, for example a
CP2102. This [manual check](engineering/verification.md#manual-checks) is not recorded as
done yet.

![Wiring a Raspberry Pi to a USB serial adapter: Pi pin 8 TXD to adapter RXD, Pi pin 10 RXD to adapter TXD, pin 6 GND to GND](images/uart-wiring.png#gh-light-mode-only)
![Wiring a Raspberry Pi to a USB serial adapter: Pi pin 8 TXD to adapter RXD, Pi pin 10 RXD to adapter TXD, pin 6 GND to GND](images/uart-wiring-dark.png#gh-dark-mode-only)

**One time setup on the Pi:**

1. `sudo raspi-config` → Interface Options → Serial Port: login shell over serial **No**,
   serial port hardware **Yes**. Reboot.
2. Add your user to `dialout` (the group allowed to use serial ports):

    ```
    sudo usermod -aG dialout $USER
    sudo systemctl restart open-battery-emulator
    ```

3. Set `interfaces.uart.enabled: true` and `port: /dev/serial0` (GPIO 14 and 15).

**Wiring** (TX to RX, RX to TX):

| Pi pin | Pi signal | Adapter pin |
|---|---|---|
| 8 | TX (GPIO 14) | RX |
| 10 | RX (GPIO 15) | TX |
| 6 | GND | GND |

!!! warning
    Do not connect the adapter's 3.3 V or 5 V pin. The Pi's pins are 3.3 V only.

**On the laptop:** `picocom -b 115200 /dev/ttyUSB0`. Lines arrive once a second; send
`$CUR` lines as in [Testing without hardware](interfaces/testing-without-hardware.md#virtual-uart),
with `/dev/ttyUSB0` instead of `/tmp/ttyOBE1`. Unplugging the adapter does not stop the
emulator; lines come back within about 2 s.

## CAN HAT

!!! note "Not tested yet"
    Physical CAN has not been tested yet. A CAN HAT or adapter should work through
    SocketCAN like `vcan0`, but no frames have been sent over real wires so far. The test
    is planned ([Later](engineering/milestones.md#later)).

1. Add the HAT's device tree overlay (tells Linux which CAN chip is on the HAT) to
   `/boot/firmware/config.txt` and reboot. For an MCP2515 HAT for example:

    ```
    dtoverlay=mcp2515-can0,oscillator=12000000,interrupt=25
    ```

    The oscillator and interrupt values depend on the HAT; check its documentation.

2. Set `interfaces.can.enabled: true`, `channel: can0` and your `bitrate`, then restart
   once (or reboot):

    ```
    sudo systemctl restart open-battery-emulator-can open-battery-emulator
    ```

??? info "Bus notes"
    The bus step sets `restart-ms 100`, so the controller recovers by itself from bus off
    (a controller stops sending after too many errors). A CAN bus needs at least one other
    device to acknowledge frames and a 120 ohm terminating resistor at each end. Without
    another device the emulator warns that sending fails and keeps running.
