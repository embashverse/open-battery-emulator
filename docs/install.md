# Install

How to install the emulator on a Raspberry Pi (it then starts at every boot) or on a Linux
laptop (you start it by hand). If something does not work, see
[Troubleshooting](troubleshooting.md).

=== "Raspberry Pi"

    1. With [Raspberry Pi Imager](https://www.raspberrypi.com/software/) write
       **Raspberry Pi OS Lite (64-bit)**. In the OS customisation set a hostname, a user
       and password, your Wi-Fi, and enable SSH.
    2. Log in from your laptop: `ssh <user>@<hostname>.local`
    3. Run the quick install:

        ```
        curl -LsSf https://raw.githubusercontent.com/embashverse/open-battery-emulator/main/deploy/bootstrap.sh | bash
        ```

        Or from a clone:

        ```
        git clone https://github.com/embashverse/open-battery-emulator.git ~/open-battery-emulator
        cd ~/open-battery-emulator
        deploy/bootstrap.sh
        ```

    At the end it prints the web UI address, `http://<hostname>.local:8080`. Open it from a
    laptop or phone on the same network. The emulator now runs as a service and starts
    at every boot.

    ??? info "What bootstrap.sh does"
        1. Installs git and can-utils (CAN tools such as `candump`).
        2. Installs [uv](https://docs.astral.sh/uv/) (installs Python and the packages)
           if missing.
        3. Uses the clone it is run from, or clones into `~/open-battery-emulator`.
        4. Runs `uv sync`, then `sudo deploy/install.sh`, which installs two services:

            | Service | Does |
            |---|---|
            | `open-battery-emulator-can` | brings the CAN bus up at boot, as root, only when CAN is enabled |
            | `open-battery-emulator` | runs the emulator as your normal user, restarts it if it stops with an error |

        It is safe to run twice and stops at the first error, naming the step.

=== "Laptop"

    Needs Linux, Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

    ```
    git clone https://github.com/embashverse/open-battery-emulator.git
    cd open-battery-emulator
    uv sync
    uv run python -m emulator.engine
    ```

    Open `http://localhost:8080`. Stop it with Ctrl+C.

    | Option | Example | Meaning |
    |---|---|---|
    | `--config` | `--config other.yaml` | settings file (default `config.yaml`) |
    | `--speed` | `--speed 10` | 10 times faster than real time. Must be 1 while an interface such as CAN is enabled |
    | `--status-every` | `--status-every 10` | print the [status line](using.md#terminal-status-line) every 10 s instead of every second |

## Managing the service (Pi)

| To | Run |
|---|---|
| check it runs | `systemctl status open-battery-emulator` (should say "active (running)") |
| watch its output | `journalctl -u open-battery-emulator -f` (status line every 10 s, Ctrl+C to leave) |
| apply a hand edit of code or `config.yaml` | `sudo systemctl restart open-battery-emulator` |
| update | `cd ~/open-battery-emulator && git pull && uv sync && sudo systemctl restart open-battery-emulator` |
| remove | `sudo deploy/install.sh --remove` (stops and disables both services, deletes their unit files; the clone stays) |

`~/open-battery-emulator` is a normal git clone, so you can edit and develop there.

## Next

CAN and UART are off by default: turn them on in the Config tab or `config.yaml`
([Configuration](configuration.md)). Try them without hardware in
[Testing without hardware](interfaces/testing-without-hardware.md), or wire real devices
with [Hardware](hardware.md).
