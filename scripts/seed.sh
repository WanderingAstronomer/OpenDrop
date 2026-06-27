#!/usr/bin/env bash
# Seed the Ohio test region so the map has real data on first boot:
#   OSM ingest (Columbus metro) + Salvation Army scrape + Goodwill (enrich, persists nothing) + dedup.
# Runs inside the api container (it has the pipeline package and DB access).
# Falls back to the committed Phase-1 OSM fixture if a live endpoint is unreachable.
set -euo pipefail

cd "$(dirname "$0")/.."
echo "Seeding OpenDrop (region: ${SEED_REGION_BBOX:-Columbus metro})..."
exec docker compose run --rm api python -m pipeline.seed
