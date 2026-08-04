#!/usr/bin/env bash
# Runs on the Mac. Pushes the current code to the Oracle server and restarts it.
#
#   ./deploy/push.sh              push code, restart, show status
#
# The server's noctua.db and .env are never touched: the database is live
# state (registrations, laundry sessions) and only ever changes on the server.
#
# Reads the server address from deploy/server.env (created during setup).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="$(dirname "$HERE")"
# shellcheck disable=SC1091
source "$HERE/server.env"   # defines SERVER_HOST, SERVER_USER, SSH_KEY

SSH=(ssh -i "$SSH_KEY" -o StrictHostKeyChecking=accept-new "$SERVER_USER@$SERVER_HOST")
REMOTE="$SERVER_USER@$SERVER_HOST:/opt/noctua-bot/"

echo "==> Syncing code to $SERVER_HOST"
rsync -az --delete \
    -e "ssh -i $SSH_KEY -o StrictHostKeyChecking=accept-new" \
    --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude 'noctua.db*' --exclude 'noctua.log' --exclude 'backups' \
    --exclude '.env' --exclude '.git' \
    "$PROJECT/bot" "$PROJECT/tests" "$PROJECT/deploy" "$PROJECT/roster" \
    "$PROJECT/requirements.txt" "$PROJECT/README.md" "$PROJECT/SPEC.md" \
    "$REMOTE"
# roster/ rides along so the master list edited on the Mac is on the server
# ready for `python -m bot.roster_sync --apply`. It is data, not code: nothing
# reads it at runtime, and the bot is restarted below either way.

echo "==> Installing dependencies and restarting"
"${SSH[@]}" '/opt/noctua-bot/.venv/bin/pip install -q -r /opt/noctua-bot/requirements.txt && sudo systemctl restart noctua-bot'
sleep 3
"${SSH[@]}" 'systemctl is-active noctua-bot && tail -n 5 /opt/noctua-bot/noctua.log'

echo
echo "Deployed."
