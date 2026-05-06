#!/usr/bin/env bash
# Nightly Postgres dump. Keeps last 14 days locally.
set -euo pipefail

DEST=/var/backups/coach
DB=coach
RETAIN=14

mkdir -p "$DEST"
ts=$(date +%F-%H%M)
file="$DEST/coach-$ts.sql.gz"

pg_dump --clean --if-exists --no-owner --no-privileges "$DB" | gzip -9 > "$file"

# Prune old dumps
find "$DEST" -name 'coach-*.sql.gz' -mtime +$RETAIN -delete

echo "$(date -Iseconds) backup wrote $file ($(stat -c%s "$file") bytes)"
