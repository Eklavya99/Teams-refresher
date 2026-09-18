#!/usr/bin/env bash
# Install the Teams refresher: udev rule, group membership, binary, user service.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
DOC_DIR="$HOME/.local/share/teams-refresher"
UNIT_DIR="$HOME/.config/systemd/user"
RULE=/etc/udev/rules.d/70-uinput-teams-refresher.rules

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }

[ "$(id -u)" -ne 0 ] || { echo "Run as your normal user, not root."; exit 1; }

say "Installing the daemon to $BIN_DIR/teams-refresher"
# Installed outside the project dir so the service still starts if this
# removable volume isn't mounted at login.
mkdir -p "$BIN_DIR" "$DOC_DIR" "$UNIT_DIR"
install -m 0755 "$SRC_DIR/teams_refresher.py" "$BIN_DIR/teams-refresher"
install -m 0644 "$SRC_DIR/README.md" "$DOC_DIR/README.md" 2>/dev/null || true

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

say "Installing the systemd user service"
install -m 0644 "$SRC_DIR/systemd/teams-refresher.service" "$UNIT_DIR/teams-refresher.service"
systemctl --user daemon-reload

if ! command -v teams-refresher >/dev/null 2>&1; then
  # ~/.profile prepends ~/.local/bin only when it already exists at login,
  # so a first-time install is picked up by the same re-login the group needs.
  NEED_RELOGIN=1
fi

echo
if [ "$NEED_RELOGIN" = 1 ]; then
  warn "Log out and back in to pick up the 'input' group and ~/.local/bin on PATH."
  warn "Then run:  systemctl --user enable --now teams-refresher"
  echo
  echo "To try it right now without logging out:"
  echo "  sg input -c '$BIN_DIR/teams-refresher --once'"
else
  say "Done. Start it with:  systemctl --user enable --now teams-refresher"
fi
echo
echo "Check state any time:  teams-refresher --status"
echo "Watch it work:         journalctl --user -u teams-refresher -f"
