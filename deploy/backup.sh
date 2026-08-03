#!/usr/bin/env bash
# Nightly SQLite backup. Keeps 14 days of snapshots.
# Uses sqlite3 .backup so it is safe while the bot is writing (WAL mode).
set -euo pipefail

APP_DIR=/opt/noctua-bot
BACKUP_DIR="$APP_DIR/backups"
STAMP=$(date +%Y-%m-%d)

mkdir -p "$BACKUP_DIR"
sqlite3 "$APP_DIR/noctua.db" ".backup '$BACKUP_DIR/noctua-$STAMP.db'"
find "$BACKUP_DIR" -name 'noctua-*.db' -mtime +14 -delete
