#!/usr/bin/env bash
# Runs ON the Oracle server, once, to prepare it for Noctua Bot.
# Usage:  bash server_setup.sh
set -euo pipefail

APP_DIR=/opt/noctua-bot

echo "==> Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-pip sqlite3 rsync

echo "==> Creating $APP_DIR"
sudo mkdir -p "$APP_DIR"
sudo chown "$USER":"$USER" "$APP_DIR"

echo "==> Creating virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip

if [ -f "$APP_DIR/requirements.txt" ]; then
    echo "==> Installing Python dependencies"
    "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi

echo "==> Installing systemd service"
sudo cp "$APP_DIR/deploy/noctua-bot.service" /etc/systemd/system/noctua-bot.service
sudo systemctl daemon-reload
sudo systemctl enable noctua-bot

echo "==> Installing nightly database backup (03:00 Singapore time)"
sudo cp "$APP_DIR/deploy/backup.sh" /usr/local/bin/noctua-backup
sudo chmod +x /usr/local/bin/noctua-backup
# `|| true` on both: an empty crontab makes `crontab -l` and `grep` exit non-zero,
# which would abort the script under `set -e`.
{ crontab -l 2>/dev/null | grep -v noctua-backup || true ; echo "0 3 * * * /usr/local/bin/noctua-backup" ; } | crontab -

echo "==> Setting timezone to Asia/Singapore"
sudo timedatectl set-timezone Asia/Singapore

echo
echo "Server prepared. Start the bot with:  sudo systemctl start noctua-bot"
