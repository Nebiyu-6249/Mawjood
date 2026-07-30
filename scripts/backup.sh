#!/usr/bin/env bash
#
# Daily logical backup of the Mawjood database.
#
# Custom-format pg_dump (-Fc): compressed, and restorable selectively with
# pg_restore, which a plain SQL dump cannot do. That matters during an incident
# when you want one table back and not the whole database.
#
#   ./scripts/backup.sh                     # uses MAWJOOD_DATABASE_URL
#   ./scripts/backup.sh --dir /var/backups  # somewhere durable
#   ./scripts/backup.sh --retention-days 30
#
# Cron (daily 02:30 Gulf time = 22:30 UTC the day before):
#   30 22 * * * /srv/mawjood/scripts/backup.sh --dir /var/backups/mawjood
#
# Retention is 30 days by default, matching docs/RUNBOOK.md. Pruning happens
# AFTER a successful dump and verification, never before: deleting old backups
# first would mean a failing dump quietly erodes the history it was meant to add
# to.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
DATABASE_URL="${MAWJOOD_DATABASE_URL:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)            BACKUP_DIR="$2"; shift 2 ;;
    --retention-days) RETENTION_DAYS="$2"; shift 2 ;;
    --url)            DATABASE_URL="$2"; shift 2 ;;
    -h|--help)        sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [[ -z "$DATABASE_URL" ]]; then
  echo "set MAWJOOD_DATABASE_URL or pass --url" >&2
  exit 2
fi

# SQLAlchemy's async driver suffix is not something libpq understands.
PG_URL="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/mawjood-${STAMP}.dump"

echo "backing up to ${TARGET}"
pg_dump --format=custom --compress=9 --no-owner --no-privileges --file="$TARGET" "$PG_URL"

# Verify before trusting it. A dump that cannot be listed cannot be restored,
# and finding that out during an incident is the worst possible time.
if ! pg_restore --list "$TARGET" > /dev/null 2>&1; then
  echo "FAILED: ${TARGET} is not a readable dump; keeping it for inspection" >&2
  exit 1
fi

SIZE="$(du -h "$TARGET" | cut -f1)"
TABLES="$(pg_restore --list "$TARGET" | grep -c 'TABLE DATA' || true)"
echo "ok: ${SIZE}, ${TABLES} tables with data"

# An empty-looking backup is almost always a connection pointed at the wrong
# database. Louder than a silent success.
if [[ "$TABLES" -eq 0 ]]; then
  echo "WARNING: no table data in the dump — is MAWJOOD_DATABASE_URL right?" >&2
fi

# Prune only now that today's backup exists and verifies.
DELETED="$(find "$BACKUP_DIR" -name 'mawjood-*.dump' -mtime "+${RETENTION_DAYS}" -print -delete | wc -l)"
echo "pruned ${DELETED} backup(s) older than ${RETENTION_DAYS} days"
