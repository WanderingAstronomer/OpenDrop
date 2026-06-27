#!/usr/bin/env bash
# Restore the OpenDrop database from a gzipped pg_dump produced by backup.sh.
#   bash scripts/restore.sh backups/opendrop-XXXX.sql.gz
set -euo pipefail
cd "$(dirname "$0")/.."
FILE="${1:?usage: restore.sh <dump.sql.gz>}"
gunzip -c "$FILE" | docker compose exec -T db psql -U "${POSTGRES_USER:-opendrop}" -d "${POSTGRES_DB:-opendrop}"
echo "restored from $FILE"
