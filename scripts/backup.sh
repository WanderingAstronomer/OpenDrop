#!/usr/bin/env bash
# OpenDrop backup — captures BOTH halves of the system's state:
#   1. the PostgreSQL/PostGIS database  (the map: locations, votes, corrections, audit)
#   2. the uploaded-photo media volume  (referenced by the DB but stored on disk)
# A DB-only backup is NOT a complete restore point — the photos would 404 after a restore.
#
#   bash scripts/backup.sh [output_dir]      # default: ./backups
#   BACKUP_RETENTION=30 bash scripts/backup.sh
#
# Produces, for timestamp TS:
#   opendrop-db-<TS>.dump        custom-format pg_dump  (restore with pg_restore)
#   opendrop-media-<TS>.tgz      tar.gz of /app/media   (skipped if no photos yet)
#   opendrop-<TS>.sha256         checksums of the two artifacts above
set -euo pipefail
cd "$(dirname "$0")/.."

OUT="${1:-backups}"
RETENTION="${BACKUP_RETENTION:-14}"          # how many timestamped sets to keep
PGUSER="${POSTGRES_USER:-opendrop}"
PGDB="${POSTGRES_DB:-opendrop}"
mkdir -p "$OUT"
TS="$(date -u +%Y%m%dT%H%M%SZ)"

DB_FILE="$OUT/opendrop-db-$TS.dump"
MEDIA_FILE="$OUT/opendrop-media-$TS.tgz"
SUMS="$OUT/opendrop-$TS.sha256"

# --- database: custom format (-Fc) so we can pg_restore with --clean/--jobs ----------------
# Write to a temp file and rename only on success, so a half-written dump is never mistaken
# for a good backup (set -o pipefail already fails the run if pg_dump errors mid-stream).
echo "[backup] dumping database '$PGDB'…"
docker compose exec -T db pg_dump -U "$PGUSER" -d "$PGDB" -Fc --no-owner --no-privileges > "$DB_FILE.tmp"
[ -s "$DB_FILE.tmp" ] || { echo "[backup] ERROR: empty database dump" >&2; rm -f "$DB_FILE.tmp"; exit 1; }
mv "$DB_FILE.tmp" "$DB_FILE"
echo "[backup]   -> $DB_FILE ($(du -h "$DB_FILE" | cut -f1))"

# --- media: tar the uploaded-photo volume from inside the api container ---------------------
# `|| true` on the find: an empty media dir is normal on a fresh install, not an error.
echo "[backup] archiving uploaded photos…"
if docker compose exec -T api sh -c 'cd /app/media 2>/dev/null && find . -type f | head -n1 | grep -q .' ; then
  docker compose exec -T api tar -C /app/media -czf - . > "$MEDIA_FILE.tmp"
  mv "$MEDIA_FILE.tmp" "$MEDIA_FILE"
  echo "[backup]   -> $MEDIA_FILE ($(du -h "$MEDIA_FILE" | cut -f1))"
else
  echo "[backup]   (no photos yet — skipping media archive)"
  MEDIA_FILE=""
fi

# --- integrity: checksum every artifact so restore can verify before trusting it -----------
( cd "$OUT" && sha256sum "$(basename "$DB_FILE")" ${MEDIA_FILE:+"$(basename "$MEDIA_FILE")"} > "$(basename "$SUMS")" )
echo "[backup]   -> $SUMS"

# --- retention: keep the newest $RETENTION db dumps and their matching media/sha files -----
echo "[backup] pruning to the newest $RETENTION sets…"
ls -1t "$OUT"/opendrop-db-*.dump 2>/dev/null | tail -n +$((RETENTION + 1)) | while read -r old; do
  stamp="$(basename "$old" | sed -E 's/^opendrop-db-(.*)\.dump$/\1/')"
  echo "[backup]   removing set $stamp"
  rm -f "$OUT/opendrop-db-$stamp.dump" "$OUT/opendrop-media-$stamp.tgz" "$OUT/opendrop-$stamp.sha256"
done

echo "[backup] done: set $TS"
