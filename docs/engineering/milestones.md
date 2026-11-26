# Milestones and Later

The plan in steps, each ending with something that runs and is checked, and ideas for
after v1.0.

## Milestones

| # | Goal | Done when |
|---|---|---|
| M0 | Setup | skeleton and green CI on `main` |
| M1 | Cell and pack | `cell.py` and `pack.py` pass their tests; 100 A for 1 h on a 100 Ah cell takes SOC from 100 % to 0 %; every state transition, cell variation, Set SOC and Hold SOC tested |
| M2 | Engine | engine runs in the terminal from `config.yaml` with simulated current and prints values |
| M3 | CAN | M3a: pack messages on `vcan0` decode with the DBC; module frames follow the pattern. M3b: 0x180 changes the current; timeout works |
| M4 | Pi | runs on the Pi as a service on boot with `vcan0`, survives a reboot, restarts after a crash |
| M5 | Web UI | M5a: optional page shows live values and changes the simulated current. M5b: Live and Config tabs, Play / Stop, settings saved to `config.yaml` (REQ-16, 17, 18, 36, 37). M5c: edits CAN message IDs and module frame IDs with checks before saving (REQ-29) |
| M6 | UART interface | frames seen and current set over a virtual serial pair (socat) on the laptop, and over the Pi's UART with a CP2102 adapter on the laptop (REQ-38 to REQ-42) |
| v1.0 | Release | runs on the Pi with virtual CAN and a physical UART link, web UI checked from a phone, every REQ is verified (a passing test, or a manual check recorded in [Verification](verification.md)), docs complete, tagged release |

## Later

Not requirements yet; each would start as a new one on the [Requirements](requirements.md)
page.

- Temperature model (temperature changing with current)
- fault injection
- NMC and other batteries
- gradual hysteresis transition (OCV moves from one curve to the other over a few % SOC instead of switching at once)
- per module capacity
- load profiles from CSV
- script that writes module frames into a DBC for decoding tools
- physical CAN test (MCP2515 HAT on the Pi and a CANable on a laptop, frames received and current sent over real wires)
- isoSPI interface (needs an LTC6820 type transceiver and tight timing, likely on an MCU rather than Linux)
- Modbus or MQTT for BESS
- cell and module signals in the DBC by naming pattern (for example `Cell_M3_C7`, `ModuleTemp_M3`), filled by module and cell number
- an optional `check(settings, pack_info)` function in the interface contract, so Save can catch interface errors like a broken DBC before Play.
- Move to Zensical before May 2027, when Material for MkDocs reaches end of life. Tried
  with Zensical 0.1.3 in November 2026: it builds these pages and fails on broken links
  and missing anchors, but does not yet fail on a page missing from the navigation, which
  CI relies on.
