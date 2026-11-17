#!/usr/bin/env bash
# Install or remove the two systemd services on a Raspberry Pi (REQ-32).
#
#   sudo deploy/install.sh              install, enable and start both services
#   sudo deploy/install.sh --remove     stop, disable and delete both services
#   deploy/install.sh --dry-run         only print what would be done (no sudo needed)
#
# --dry-run also works together with --remove.

set -euo pipefail

# --- 1. Names and paths -------------------------------------------------------

CAN_UNIT="open-battery-emulator-can.service"
EMULATOR_UNIT="open-battery-emulator.service"
UNITS=("$CAN_UNIT" "$EMULATOR_UNIT")
SYSTEMD_DIR="/etc/systemd/system"

# The repository is the folder above this script, wherever it was cloned.
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPLOY_DIR="$REPO/deploy"

# --- 2. Options ---------------------------------------------------------------

REMOVE=false
DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --remove) REMOVE=true ;;
        --dry-run) DRY_RUN=true ;;
        -h|--help)
            sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Unknown option: $arg (use --remove and/or --dry-run)" >&2
            exit 1
            ;;
    esac
done

# --- 3. Helpers ---------------------------------------------------------------

# Run a command, or only print it with --dry-run.
run() {
    if $DRY_RUN; then
        echo "would run: $*"
    else
        echo "+ $*"
        "$@"
    fi
}

# Print a unit file with @USER@ and @REPO@ filled in. "|" separates the sed
# parts because the repo path contains "/".
filled_unit() {
    sed -e "s|@USER@|$SERVICE_USER|g" -e "s|@REPO@|$REPO|g" "$DEPLOY_DIR/$1"
}

# --- 4. Checks ----------------------------------------------------------------

# The emulator runs as the user who called sudo, never as root (REQ-31).
SERVICE_USER="${SUDO_USER:-$(id -un)}"

if ! $DRY_RUN; then
    if [[ $EUID -ne 0 ]]; then
        echo "Run this with sudo: sudo $0 $*" >&2
        exit 1
    fi
    if [[ -z "${SUDO_USER:-}" || "$SUDO_USER" == "root" ]]; then
        echo "Run this with sudo from your normal user, not as root:" >&2
        echo "the emulator service runs as the user who called sudo." >&2
        exit 1
    fi
fi

# --- 5. Remove ----------------------------------------------------------------

if $REMOVE; then
    # Stop the emulator first, then the bus step. "|| true": already stopped,
    # disabled or missing is fine when removing.
    run systemctl stop "$EMULATOR_UNIT" "$CAN_UNIT" || true
    run systemctl disable "${UNITS[@]}" || true
    for unit in "${UNITS[@]}"; do
        run rm -f "$SYSTEMD_DIR/$unit"
    done
    run systemctl daemon-reload
    if ! $DRY_RUN; then
        echo "Removed. The CAN device (for example vcan0) stays up until the next reboot."
    fi
    exit 0
fi

# --- 6. Install ---------------------------------------------------------------

if [[ ! -x "$REPO/.venv/bin/python" ]]; then
    echo "No $REPO/.venv found. Run 'uv sync' in $REPO first (as $SERVICE_USER)." >&2
    exit 1
fi

for unit in "${UNITS[@]}"; do
    if $DRY_RUN; then
        echo "would write $SYSTEMD_DIR/$unit:"
        filled_unit "$unit" | sed 's/^/    /'
    else
        echo "+ write $SYSTEMD_DIR/$unit"
        filled_unit "$unit" > "$SYSTEMD_DIR/$unit"
    fi
done

run systemctl daemon-reload
run systemctl enable "${UNITS[@]}"
run systemctl start "$CAN_UNIT" "$EMULATOR_UNIT"

if ! $DRY_RUN; then
    echo
    echo "Installed. The emulator runs as $SERVICE_USER and starts at every boot."
    echo "Check it:  systemctl status $EMULATOR_UNIT"
    echo "Logs:      journalctl -u $EMULATOR_UNIT -f"
fi
