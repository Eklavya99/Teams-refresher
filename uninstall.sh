#!/usr/bin/env bash
# Remove the Linux uinput permissions granted by install.sh.
# The daemon and its login service are removed the same way everywhere:
#   teams-refresher --uninstall-service && pip uninstall teams-refresher
set -euo pipefail
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "Stopping and removing the login service"
if command -v teams-refresher >/dev/null 2>&1; then
  teams-refresher --uninstall-service || true
else
  python3 -m teams_refresher --uninstall-service || true
fi

say "Removing the udev rule (needs sudo)"
sudo rm -f /etc/udev/rules.d/70-uinput-teams-refresher.rules /etc/modules-load.d/uinput.conf
sudo udevadm control --reload-rules

# Left over from version 1, when the daemon was a single file copied by hand.
rm -f "$HOME/.local/bin/teams-refresher"
rm -rf "$HOME/.local/share/teams-refresher"

echo
echo "Still installed as a Python package; remove it with:"
echo "  pip uninstall teams-refresher"
echo
echo "Left alone on purpose: your membership of the 'input' group."
echo "Remove it yourself if you want:  sudo gpasswd -d $USER input"
