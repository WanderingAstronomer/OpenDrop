#!/usr/bin/env bash
# OpenDrop restore — inverse of backup.sh. Restores the database (and optionally the photo
# media volume) from a timestamped backup set.
#
#   bash scripts/restore.sh <opendrop-db-TS.dump> [opendrop-media-TS.tgz] [--db NAME] [--force]
#
#   --db NAME   restore into database NAME instead of the live $POSTGRES_DB. Use a scratch DB
#               (e.g. opendrop_drill) to rehearse a restore without touching production.
#   --force     allow restoring over a database that already contains locations. Without it,
#               restore REFUSES to overwrite populated data — a guard against fat-finger DR.
#
# pg_restore runs with --clean --if-exists, so it drops and recreates the dump's objects in place.
set -euo pipefail
cd "$(dirname "$0")/.."

PGUSER="${POSTGRES_USER:-opendrop}"
TARGET_DB="${POSTGRES_DB:-opendrop}"
FORCE=0
DB_FILE=""
MEDIA_FILE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --db)    TARGET_DB="${2:?--db needs a name}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    *.tgz|*.tar.gz) MEDIA_FILE="$1"; shift ;;
    *)       DB_FILE="$1"; shift ;;
  esac
done
[ -n "$DB_FILE" ] || { echo "usage: restore.sh <db.dump> [media.tgz] [--db NAME] [--force]" >&2; exit 2; }
[ -f "$DB_FILE" ] || { echo "ERROR: no such dump: $DB_FILE" >&2; exit 2; }

# --- verify integrity against the set's .sha256 if one sits beside the dump -----------------
TS="$(basename "$DB_FILE" | sed -E 's/^opendrop-db-(.*)\.dump$/\1/')"
SUMS="$(dirname "$DB_FILE")/opendrop-$TS.sha256"
if [ -f "$SUMS" ]; then
  echo "[restore] verifying checksums…"
  ( cd "$(dirname "$DB_FILE")" && sha256sum -c "$(basename "$SUMS")" ) || {
    echo "[restore] ERROR: checksum mismatch — refusing to restore a corrupt backup" >&2; exit 1; }
else
  echo "[restore] WARNING: no .sha256 beside the dump; cannot verify integrity"
fi

# --- safety guard: don't silently overwrite a populated database ----------------------------
EXISTS="$(docker compose exec -T db psql -U "$PGUSER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='$TARGET_DB'" 2>/dev/null || true)"
if [ "$EXISTS" = "1" ]; then
  ROWS="$(docker compose exec -T db psql -U "$PGUSER" -d "$TARGET_DB" -tAc \
    "SELECT count(*) FROM locations" 2>/dev/null | tr -d '[:space:]' || echo 0)"
  if [ "${ROWS:-0}" -gt 0 ] && [ "$FORCE" -ne 1 ]; then
    echo "[restore] ABORT: '$TARGET_DB' already has $ROWS locations. Re-run with --force to overwrite," >&2
    echo "          or pass --db opendrop_drill to restore into a scratch database instead." >&2
    exit 1
  fi
else
  echo "[restore] creating database '$TARGET_DB'…"
  docker compose exec -T db createdb -U "$PGUSER" "$TARGET_DB"
fi

# --- restore the database -------------------------------------------------------------------
echo "[restore] restoring database into '$TARGET_DB'…"
docker compose exec -T db pg_restore -U "$PGUSER" -d "$TARGET_DB" --clean --if-exists --no-owner < "$DB_FILE"
echo "[restore]   database restored ($(docker compose exec -T db psql -U "$PGUSER" -d "$TARGET_DB" -tAc \
  'SELECT count(*) FROM locations' | tr -d '[:space:]') locations)"

# --- restore media if a media archive was given ---------------------------------------------
if [ -n "$MEDIA_FILE" ]; then
  [ -f "$MEDIA_FILE" ] || { echo "ERROR: no such media archive: $MEDIA_FILE" >&2; exit 2; }
  echo "[restore] restoring photos into the media volume…"
  docker compose exec -T api sh -c 'mkdir -p /app/media && tar -C /app/media -xzf -' < "$MEDIA_FILE"
  echo "[restore]   media restored"
fi

echo "[restore] done."
