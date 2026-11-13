#!/usr/bin/env bash
# Set up the music box on Raspberry Pi OS (Bookworm or later).
# Run from the repo directory as your normal user:  ./install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
RUN_USER="${SUDO_USER:-$USER}"

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y mpv python3-venv python3-dev i2c-tools

echo "==> Enabling I2C"
sudo raspi-config nonint do_i2c 0

echo "==> Installing musicbox into $DIR/.venv"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -e "$DIR[pi]"

echo "==> Installing systemd service (runs as $RUN_USER)"
sed -e "s|@USER@|$RUN_USER|g" -e "s|@DIR@|$DIR|g" "$DIR/systemd/musicbox.service" \
    | sudo tee /etc/systemd/system/musicbox.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable musicbox.service

echo
echo "Done. Next: mount the drive, assign cards, then"
echo "  sudo systemctl start musicbox"
echo "See README.md for details."
