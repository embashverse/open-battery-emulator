# Troubleshooting

Common problems and how to fix them. Find what you see in the left column. Error texts
are shown as the emulator prints them, so you can search this page for them.

| You see | Do this |
|---|---|
| `interfaces.uart.port: cannot open /dev/ttyUSB0 (...)` | **Wrong port:** list ports with `ls /dev/ttyUSB* /dev/serial*` and set `interfaces.uart.port`. **Adapter unplugged:** plug it in, press Play. **No permission:** `sudo usermod -aG dialout $USER`, then log out and in (on the Pi: `sudo systemctl restart open-battery-emulator`) |
| `interfaces.uart.port: cannot open /tmp/ttyOBE0` | `socat` stopped (closed terminal, Ctrl+C, reboot), so the virtual ports are gone. Start it again ([command](interfaces/testing-without-hardware.md#virtual-uart)), then press Play |
| `interfaces.can.channel: cannot open vcan0 ...` after a reboot (laptop) | `vcan0` does not survive a reboot on a laptop. Create it again ([commands](interfaces/testing-without-hardware.md#virtual-can)), then press Play |
| same error on the Pi | the bus step ran before CAN was enabled or the channel changed: `sudo systemctl restart open-battery-emulator-can open-battery-emulator` |
| Web UI does not open on a phone | phone and Pi on the same Wi-Fi; `webui.host: 0.0.0.0` (not `127.0.0.1`); use `http://<hostname>.local:8080` or the Pi's IP address (`hostname -I` on the Pi); if a firewall runs, allow port 8080 (for example `sudo ufw allow 8080/tcp`) |
| Amber **Timeout** pill, current stays 0 A | the source is `can` or `uart` and no command arrived within `current.timeout_ms`. Send commands faster than that ([examples](interfaces/testing-without-hardware.md)), or set the source to `simulated` |
| `interfaces.uart.baudrate: ... baud carries ... bytes ...` at Play | the lines of one cycle do not fit the baud rate ([line budget](interfaces/uart.md#line-budget)). Raise `baudrate` (both ends), raise `cycle_ms`, or set `modules: false` |
| `save_config: press Stop before saving settings` | settings can only be saved while stopped. Press Stop, Save, then Play |
| Save refused, reload notice in the Save card | `config.yaml` changed on disk since the page loaded it. Press Reload, redo your change, Save |
| After boot the web UI shows Stopped with an error | a setting or device failed at autostart, for example CAN enabled without a HAT or a missing USB adapter. Read the error under the run button, fix the setting in the Config tab (or plug in the device), Save, Play |
| `current.source '...' must be 'simulated' or the name of an enabled interface` | the source names an interface that is off. Enable it, or set the source to `simulated` |
| `--speed must be 1 when an interface is enabled` | run at real time while CAN or UART is on, or disable them for fast runs |

Still stuck? On the Pi, read the full output with `journalctl -u open-battery-emulator -f`.
