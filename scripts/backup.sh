#!/usr/bin/env bash
# Timestamped, gzipped pg_dump of the OpenDrop database.
#   bash scripts/backup.sh [output_dir]   (default: ./backups)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="${1:-backups}"
mkdir -p "$OUT"
TS="$(date -u +%Y%m%dT%H%M%SZ)"
FILE="$OUT/opendrop-$TS.sql.gz"
docker compose exec -T db pg_dump -U "${POSTGRES_USER:-opendrop}" -d "${POSTGRES_DB:-opendrop}" | gzip > "$FILE"
echo "wrote $FILE ($(du -h "$FILE" | cut -f1))"
