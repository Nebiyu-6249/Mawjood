#!/usr/bin/env bash
#
# Restore a Mawjood backup, and prove it worked.
#
#   ./scripts/restore.sh --file backups/mawjood-20260730T223000Z.dump --into mawjood_restore_check
#
# Restores into a SEPARATE database by default and refuses to touch the one
# named by MAWJOOD_DATABASE_URL unless --force-into-live is passed. A restore
# script that can silently overwrite production is a foot-gun on a timer;
# --force-into-live makes destroying live data something you have to type.
#
# After restoring it runs verification queries and prints row counts, because
# "pg_restore exited 0" is not the same as "the data is there" — a dump taken
# against an empty database restores perfectly and tells you nothing.
set -euo pipefail

DUMP_FILE=""
TARGET_DB="mawjood_restore_check"
FORCE_LIVE=0
DATABASE_URL="${MAWJOOD_DATABASE_URL:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)             DUMP_FILE="$2"; shift 2 ;;
    --into)             TARGET_DB="$2"; shift 2 ;;
    --url)              DATABASE_URL="$2"; shift 2 ;;
    --force-into-live)  FORCE_LIVE=1; shift ;;
    -h|--help)          sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$DUMP_FILE" ]] || { echo "--file is required" >&2; exit 2; }
[[ -f "$DUMP_FILE" ]] || { echo "no such dump: $DUMP_FILE" >&2; exit 2; }
[[ -n "$DATABASE_URL" ]] || { echo "set MAWJOOD_DATABASE_URL or pass --url" >&2; exit 2; }

PG_URL="${DATABASE_URL/postgresql+asyncpg:/postgresql:}"
LIVE_DB="$(basename "${PG_URL%%\?*}")"
ADMIN_URL="${PG_URL%/*}/postgres"

if [[ "$TARGET_DB" == "$LIVE_DB" && "$FORCE_LIVE" -ne 1 ]]; then
  echo "refusing to restore over the live database '${LIVE_DB}'." >&2
  echo "pass --force-into-live if that is genuinely what you want." >&2
  exit 2
fi

echo "restoring ${DUMP_FILE} into ${TARGET_DB}"
psql -q "$ADMIN_URL" -c "DROP DATABASE IF EXISTS ${TARGET_DB};"
psql -q "$ADMIN_URL" -c "CREATE DATABASE ${TARGET_DB};"

TARGET_URL="${PG_URL%/*}/${TARGET_DB}"
pg_restore --no-owner --no-privileges --dbname="$TARGET_URL" "$DUMP_FILE"

echo
echo "verification —"
psql -q "$TARGET_URL" <<'SQL'
\pset border 2
SELECT 'tenants'      AS table, count(*) FROM tenants
UNION ALL SELECT 'leads',           count(*) FROM leads
UNION ALL SELECT 'conversations',   count(*) FROM conversations
UNION ALL SELECT 'messages',        count(*) FROM messages
UNION ALL SELECT 'bookings',        count(*) FROM bookings
UNION ALL SELECT 'consents',        count(*) FROM consents
UNION ALL SELECT 'audit_log',       count(*) FROM audit_log
UNION ALL SELECT 'scheduled_notifs', count(*) FROM scheduled_notifications
ORDER BY 1;
SQL

# The schema version has to come back too. A restore that loses alembic_version
# leaves a database nothing can migrate.
echo
echo "schema version: $(psql -tAq "$TARGET_URL" -c 'SELECT version_num FROM alembic_version;')"

# The append-only trigger on audit_log is part of the schema, not the data. If it
# did not come back, the restored database would silently accept audit deletions.
TRIGGER="$(psql -tAq "$TARGET_URL" -c \
  "SELECT count(*) FROM pg_trigger WHERE tgrelid = 'audit_log'::regclass AND NOT tgisinternal;")"
if [[ "$TRIGGER" -lt 1 ]]; then
  echo "FAILED: audit_log append-only trigger did not survive the restore" >&2
  exit 1
fi
echo "audit_log append-only trigger: present"
echo
echo "restore verified into ${TARGET_DB}"
