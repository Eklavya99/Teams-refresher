#!/usr/bin/env bash
# Remove everything install.sh created.
set -euo pipefail
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

say "Stopping the service"
systemctl --user disable --now teams-refresher.service 2>/dev/null || true
rm -f "$HOME/.config/systemd/user/teams-refresher.service"
systemctl --user daemon-reload 2>/dev/null || true

say "Removing the binary and docs"
rm -f "$HOME/.local/bin/teams-refresher"
rm -rf "$HOME/.local/share/teams-refresher"

say "Removing the udev rule (needs sudo)"
sudo rm -f /etc/udev/rules.d/70-uinput-teams-refresher.rules /etc/modules-load.d/uinput.conf
sudo udevadm control --reload-rules

echo
echo "Left alone on purpose: your membership of the 'input' group."
echo "Remove it yourself if you want:  sudo gpasswd -d $USER input"
