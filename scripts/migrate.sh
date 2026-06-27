#!/usr/bin/env bash
# Apply migrations/*.sql idempotently via a schema_migrations ledger.
# Each migration file self-records (INSERT ... ON CONFLICT DO NOTHING), so re-running is a no-op.
# Intended for EXISTING databases; first container boot applies 0001 via the initdb mount.
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL (e.g. postgresql://opendrop:opendrop@localhost:5432/opendrop)}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c \
  "CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());"

for f in "$DIR"/migrations/*.sql; do
  version="$(basename "$f")"
  applied="$(psql "$DATABASE_URL" -tAc "SELECT 1 FROM schema_migrations WHERE version='$version'" | tr -d '[:space:]')"
  if [ "$applied" = "1" ]; then
    echo "skip  $version (already applied)"
  else
    echo "apply $version"
    psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$f"   # file self-records in the ledger
  fi
done
echo "migrations up to date."
