#!/usr/bin/env bash
# Quick install on a fresh Raspberry Pi OS Lite (REQ-43).
#
# Run it as your normal user (not with sudo), either from a clone:
#
#   deploy/bootstrap.sh
#
# or, once the repository is public, straight from GitHub:
#
#   curl -LsSf https://raw.githubusercontent.com/embashverse/open-battery-emulator/main/deploy/bootstrap.sh | bash
#
# It installs git and can-utils, installs uv if missing, gets the code (the clone
# it is run from, or ~/open-battery-emulator), runs uv sync and then
# sudo deploy/install.sh. Safe to run twice. Stops at the first error.

set -euo pipefail

# --- 1. Names and paths -------------------------------------------------------

REPO_URL="https://github.com/embashverse/open-battery-emulator.git"
DEFAULT_REPO="$HOME/open-battery-emulator"
TOTAL_STEPS=5

# --- 2. Helpers ---------------------------------------------------------------

CURRENT_STEP="start"

# Print the step that starts now, so the output reads like a checklist.
step() {
    CURRENT_STEP="$1/$TOTAL_STEPS $2"
    echo
    echo "==> Step $CURRENT_STEP"
}

# set -e stops the script at the first failing command; this says where.
trap 'echo; echo "Failed at step $CURRENT_STEP. Nothing after it was done." >&2' ERR

# --- 3. Checks ----------------------------------------------------------------

# install.sh runs the emulator as the user who called sudo, so this script
# must run as that normal user and call sudo itself.
if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user, not as root or with sudo." >&2
    echo "It asks for sudo itself where needed." >&2
    exit 1
fi

# --- 4. System packages -------------------------------------------------------

step 1 "Install git and can-utils (apt)"
sudo apt-get update
sudo apt-get install -y git can-utils

# --- 5. uv ------------------------------------------------------------------

step 2 "Install uv if missing"
# The uv installer puts uv in ~/.local/bin, which is not on PATH until the next
# login, so add it for the rest of this script.
export PATH="$HOME/.local/bin:$PATH"
if command -v uv > /dev/null; then
    echo "uv is already installed: $(uv --version)"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    echo "Installed $(uv --version)"
fi

# --- 6. The code --------------------------------------------------------------

step 3 "Get the code"
# Run from a clone: use that clone. Run through curl | bash there is no script
# file, so BASH_SOURCE does not point into a clone and ~/open-battery-emulator
# is used.
SCRIPT_DIR=""
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

if [[ -n "$SCRIPT_DIR" && -d "$SCRIPT_DIR/../.git" && -f "$SCRIPT_DIR/install.sh" ]]; then
    REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
    echo "Using the clone this script is in: $REPO"
elif [[ -d "$DEFAULT_REPO/.git" ]]; then
    REPO="$DEFAULT_REPO"
    # Not pulled: local changes are never touched. Update with git pull yourself.
    echo "Using the existing clone: $REPO"
elif [[ -e "$DEFAULT_REPO" ]]; then
    echo "$DEFAULT_REPO exists but is not a git clone. Move it away and run this again." >&2
    false
else
    git clone "$REPO_URL" "$DEFAULT_REPO"
    REPO="$DEFAULT_REPO"
fi

# --- 7. Python and dependencies -----------------------------------------------

step 4 "Install Python and the dependencies (uv sync)"
cd "$REPO"
uv sync

# --- 8. Services --------------------------------------------------------------

step 5 "Install and start the services (sudo deploy/install.sh)"
sudo "$REPO/deploy/install.sh"

# --- 9. Done ------------------------------------------------------------------

# The web UI settings come from config.yaml, read with the project's Python.
WEBUI="$("$REPO/.venv/bin/python" - "$REPO/config.yaml" << 'EOF'
import sys
from ruamel.yaml import YAML

webui = YAML(typ="safe").load(open(sys.argv[1])).get("webui") or {}
print(webui.get("enabled") is True, webui.get("port", ""))
EOF
)"
read -r WEBUI_ENABLED WEBUI_PORT <<< "$WEBUI"

echo
echo "==> Done. The emulator runs now and starts at every boot."
if [[ "$WEBUI_ENABLED" == "True" ]]; then
    echo "Web UI: http://$(hostname).local:$WEBUI_PORT"
    echo "(open it from a laptop or phone on the same network)"
else
    echo "The web UI is off (webui.enabled in $REPO/config.yaml)."
fi
