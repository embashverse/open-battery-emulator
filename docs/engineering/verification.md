# Verification

How each requirement is checked: by an automated test where possible, otherwise by a manual
check recorded here.

## How requirements are checked

Every requirement on the left has a check on the right:

| Define | Check |
|---|---|
| Idea | Runs on the Pi with virtual CAN and a physical UART link (v1.0) |
| Requirements | Each REQ has at least one test, or a manual check where a test is not possible |
| High level (`engine.py`) | `test_engine.py`: run many ticks from a config, check the whole result |
| Mid level (`pack.py`) | `test_pack.py`: topology, current split, state machine |
| Low level (`cell.py`) | `test_cell.py`: CSV loading, OCV, SOC, voltage, limits |

Rules:

1. Every feature starts as a requirement on the [Requirements](requirements.md) page.
2. Every test names the requirement it checks: `"""Checks REQ-04."""`
   See coverage with `uv run python -m tools.traceability` (see
   [Traceability table](#traceability-table)).
3. One task per branch, merged by pull request. CI must be green.
4. AI writes code, a human reviews and understands it before merging.
5. Every requirement is verified by an automated test where possible, otherwise by a
   manual check recorded in the table below.

Run the same checks as CI (which also runs `mkdocs build --strict`):

```
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Manual checks

Some things only a real Pi with systemd can show: booting, rebooting, crashes. These are
run by hand, following [Install](../install.md), and recorded here. The unit file
contents are also checked automatically in `tests/test_deploy.py`.

| REQ | Steps | Expected | Date | Result |
|---|---|---|---|---|
| REQ-32 | `sudo deploy/install.sh` | both services installed, enabled and started, no errors |19.11.2026 |Pass |
| REQ-31 | `systemctl status open-battery-emulator` | active (running), as the normal user (not root); `journalctl -u open-battery-emulator` shows a status line every 10 s |19.11.2026 |Pass |
| REQ-31 | `sudo reboot`, log in, `systemctl status open-battery-emulator` | running again after the reboot, `candump vcan0` shows frames |19.11.2026 |Pass |
| REQ-31 | `sudo systemctl kill -s KILL open-battery-emulator`, wait, `systemctl status open-battery-emulator` | running again within about 5 s, with a new PID |19.11.2026 |Pass |
| REQ-32 | `sudo deploy/install.sh --remove` | both services stopped and disabled, unit files gone from `/etc/systemd/system` |19.11.2026 |Pass |
| REQ-15 | `webui.enabled: true`, start the emulator, open `http://localhost:8080` on the laptop | values update about twice a second, the cell grid shows every module, Set SOC / Hold SOC / simulated current change the values on the next update, SOC 150 shows an error |25.11.2026 |Pass |
| REQ-15 | on the Pi, open `http://<hostname>.local:8080` on a phone on the same network (`<hostname>` is the name set in Raspberry Pi Imager) | page readable without zooming, controls usable, tapping a cell shows its voltage |27.11.2026 |Pass |
| REQ-39, REQ-40 | laptop: `socat -d -d pty,raw,echo=0,link=/tmp/ttyOBE0 pty,raw,echo=0,link=/tmp/ttyOBE1`, `interfaces.uart` enabled with `port: /tmp/ttyOBE0` and `current.source: uart`, start the emulator, `picocom -b 115200 /tmp/ttyOBE1`, then `printf '$CUR,50*6D\r\n' > /tmp/ttyOBE1` once a second | `$PACK`, `$CELL` and `$LIM` once a second in picocom; the status line shows 50 A while the `$CUR` lines come, then TIMEOUT and 0 A within 0.5 s after they stop |26.11.2026 |Pass |
| REQ-39, REQ-40, REQ-41 | Pi: UART enabled with `raspi-config`, user in `dialout`, `port: /dev/serial0`, `current.source: uart`. CP2102 adapter on the laptop, wired Pi TX (GPIO 14, pin 8) to adapter RX, Pi RX (GPIO 15, pin 10) to adapter TX, GND (pin 6) to GND. On the laptop `picocom -b 115200 /dev/ttyUSB0` and send `$CUR,50*6D` lines; unplug the adapter for a few seconds and plug it back | lines arrive on the laptop, the Pi's status line shows 50 A; unplugging does not stop the emulator, lines come back within about 2 s of plugging in |27.11.2026 |Pass |
| REQ-36 | with CAN on `vcan0` and `candump vcan0` running, press Stop, then Play | Stop: frames stop, run button shows a grey dot and Stopped, Config fields editable. Play: frames again, run button shows a green dot and Running, fields locked |25.11.2026 |Pass |
| REQ-16 | STOPPED, Config tab: change cells per module, Save, Play | "Saved" message, Live tab shows the new grid, `config.yaml` keeps its comments; a value like cells per module 0 shows the error and the file is unchanged |25.11.2026 |Pass |
| REQ-17 | Config tab open, edit `config.yaml` in an editor and save it | within a few seconds a reload notice appears in the Save card; Reload shows the new value |25.11.2026 |Pass |
| REQ-43, REQ-44 | fresh Raspberry Pi OS Lite: quick install with `deploy/bootstrap.sh`, then `sudo reboot`, then open the printed web UI address from a phone | every step finishes, the address is printed; after the reboot the web UI opens on the phone, RUNNING, no errors shown |27.11.2026 |Pass |
| REQ-19 | laptop: CAN on `vcan0`, `candump vcan0` (as in the REQ-36 row); Pi: after `sudo reboot`, `candump vcan0` (as in the REQ-31 row) | frames shown on both |19.11.2026 (Pi), 25.11.2026 (laptop) |Pass |

## Traceability table

The table below is generated during the docs build; nothing generated is committed. A
MkDocs hook (`hooks:` in `mkdocs.yml`, loading `tools/traceability.py`) reads three
sources and puts the table in place of a marker line on this page:

- every REQ ID and its text from [Requirements](requirements.md)
- every test whose docstring starts with `Checks REQ-xx` (or several IDs, `Checks REQ-01,
  REQ-02`) in `tests/`
- the [manual checks](#manual-checks) table above, with date and result

Each requirement gets one status:

| Status | Meaning |
|---|---|
| tests | at least one test names it |
| manual | it has manual check rows, all with a result |
| pending | it has manual check rows, at least one without a result yet |
| deferred | in the Deferred section of Requirements, not built yet |
| not verifiable | no test and no manual check row |

`tests/test_traceability.py` fails CI if any requirement is not verifiable, or if a test or
manual check names a REQ ID that does not exist (REQ-45). Pending and deferred do not fail
CI. So every new requirement needs a test with `Checks REQ-xx` or a manual check row.

For a short summary in the terminal (counts per status, and which manual checks are still
missing):

```
uv run python -m tools.traceability
```

<!-- traceability-table -->
