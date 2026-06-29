#!/usr/bin/env bash
# Seed the ENTIRE US — 50 states + DC — gently and resumably.
#
# This is a long, deliberate, overnight-scale job: it sweeps ~42k ZIPs across the ZIP-based
# sources plus per-state OSM/grid queries, all paced and backed off so it stays a good citizen.
# It checkpoints each state in the seed_progress table, so if it's interrupted you can just run
# it again and it resumes where it left off.
#
# Tune the pace with SCRAPER_REQUEST_DELAY_S (seconds between requests; default 0.5). Raise it to
# be gentler / run longer; the whole job is roughly delay x request-count.
#
# Runs inside the api container (it has the pipeline package + DB access). Make sure the stack is
# up first:  docker compose up -d
set -euo pipefail

cd "$(dirname "$0")/.."
echo "Seeding the entire US (50 states + DC). This runs overnight; it is resumable — re-run to continue."
echo "Pace: SCRAPER_REQUEST_DELAY_S=${SCRAPER_REQUEST_DELAY_S:-0.5}s between requests."
exec docker compose run --rm api python -m pipeline.seed_national
