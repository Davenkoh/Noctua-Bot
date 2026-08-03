#!/usr/bin/env bash
# Runs on the Mac. Pushes the current code to the Oracle server and restarts it.
#
#   ./deploy/push.sh              push code, restart, show status
#   ./deploy/push.sh --with-db    also upload the local noctua.db (first deploy only)
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
    "$PROJECT/bot" "$PROJECT/tests" "$PROJECT/deploy" \
    "$PROJECT/requirements.txt" "$PROJECT/README.md" "$PROJECT/SPEC.md" \
    "$REMOTE"

if [ "${1:-}" = "--with-db" ]; then
    echo "==> Uploading local database (one time only)"
    "${SSH[@]}" 'sudo systemctl stop noctua-bot || true'
    scp -i "$SSH_KEY" "$PROJECT/noctua.db" "$REMOTE"
fi

echo "==> Installing dependencies and restarting"
"${SSH[@]}" '/opt/noctua-bot/.venv/bin/pip install -q -r /opt/noctua-bot/requirements.txt && sudo systemctl restart noctua-bot'
sleep 3
"${SSH[@]}" 'systemctl is-active noctua-bot && tail -n 5 /opt/noctua-bot/noctua.log'

echo
echo "Deployed."
