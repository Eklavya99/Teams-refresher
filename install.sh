#!/usr/bin/env bash
# Linux only, and only the part that needs root: permission to create a
# virtual input device. Everything else -- installing the package and the
# login service -- is the same three commands on all three operating systems
# and lives in the README.
set -euo pipefail

RULE=/etc/udev/rules.d/70-uinput-teams-refresher.rules

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

[ "$(uname -s)" = Linux ] || {
  echo "This script is for Linux. macOS and Windows need no privileged setup:"
  echo "  macOS   -- grant Accessibility permission when asked"
  echo "  Windows -- nothing at all"
  exit 1
}
[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user, not root."; exit 1; }

say "Granting access to /dev/uinput (needs sudo, one time)"
sudo modprobe uinput
echo 'uinput' | sudo tee /etc/modules-load.d/uinput.conf >/dev/null
sudo tee "$RULE" >/dev/null <<'RULEEOF'
# Let members of the 'input' group create virtual input devices.
KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"
RULEEOF
sudo udevadm control --reload-rules
sudo udevadm trigger --name-match=uinput

if ! id -nG "$USER" | grep -qw input; then
  say "Adding $USER to the 'input' group"
  sudo usermod -aG input "$USER"
  NEED_RELOGIN=1
else
  NEED_RELOGIN=0
fi

echo
say "Done. Now install the daemon itself:"
echo "  pip install --user ."
echo "  teams-refresher --install-service"
echo
if [ "$NEED_RELOGIN" = 1 ]; then
  warn "Log out and back in first, to pick up the 'input' group."
  echo "To try it right now without logging out:"
  echo "  sg input -c 'python3 -m teams_refresher --once'"
fi
echo "Check state any time:  teams-refresher --status"
