# OpenDrop

> Community-owned, open-data map of every clothing donation location in the United States — drop bins, thrift stores, mutual aid closets, church drives, seasonal collection points. Civic infrastructure, not a product.

OpenDrop seeds a map from **redistributable** first-party sources (OpenStreetMap, The Salvation Army, Planet Aid, USAgain, and Wearable Collections), deduplicates them with a validated geo + fuzzy-name algorithm, and keeps the data fresh via **crowd validation** — confirm/deny votes, community photos, and photo-validated pin corrections — plus scheduled re-sync with closure detection. Every write path (votes, submissions, photo uploads, photo votes) is gated by Cloudflare Turnstile and per-IP cooldowns — no user accounts.

- **Stack:** PostgreSQL 17 + PostGIS · Python 3.12 / FastAPI · vanilla-JS Leaflet · Docker Compose.
- **How it was built:** Phase 1 research ([`research/FINDINGS.md`](research/FINDINGS.md)) → Phase 2 architecture ([`planning/`](planning/)) → Phase 3 construction → Phase 4 validation. The directive is [`AGENTS.md`](AGENTS.md).

## Run it (setup → running map, 7 steps)

**Prerequisites:** Docker Desktop (with Compose v2) running.

```bash
# 1. Get the code
git clone https://github.com/WanderingAstronomer/OpenDrop.git
cd OpenDrop

# 2. Create your env file (dev-safe defaults; uses Cloudflare Turnstile TEST keys)
cp .env.example .env

# 3. Build & start Postgres/PostGIS + API + web (nginx)
docker compose up -d --build

# 4. Seed real Ohio donation data (OSM + Salvation Army + Planet Aid + USAgain
#    + Wearable Collections, deduped; Goodwill enrich-only)
bash scripts/seed.sh

# 5. Open the map
#    -> http://localhost:8080
```

That's it. The map loads donation locations for the Columbus, OH metro — the default region; set `REGION=ohio` for statewide, `REGION=greater_ohio` for the multi-state region, any **two-letter state code** (`REGION=ca`), or `REGION=usa` for the whole country (see [Features](#features) and [National coverage](#national-coverage)). The map opens framed on whatever data is actually loaded — it reads the live coverage bbox from `/api/meta` and fits to it, so a Columbus seed opens on Columbus and a national seed opens on the continent. Click a pin for details and to **confirm / deny** it (the confidence score updates live). Use **＋ Add location** to submit a new spot.

To stop: `docker compose down` (add `-v` to also wipe the database volume).

### Features
- **Find / donate / resell:** charity stores, thrift, drop bins, donation centers, mutual aid, and **consignment/resale** shops (sell, don't just donate). The **List** panel is keyboard- and screen-reader-accessible with a category filter.
- **Search & locate:** address/city search box (a keyboard-navigable listbox — arrow keys + Enter) and a "use my location" control.
- **Add a location your way:** submit by typing an address, or switch to **📍 Drop a pin** and drag a marker — the street address auto-fills by reverse geocoding.
- **Accessible & themed:** light/dark themes (auto + manual toggle); dialogs trap focus, restore it on close, and expose state to assistive tech; text meets WCAG AA contrast.
- **Crowd validation:** confirm/deny votes drive a confidence score; the community can retire dead spots (retirement needs deny support to *strictly* outweigh confirmations, scaled by an engagement tier).
- **Drag-to-fix pin corrections:** open a location and **📍 Fix location** to drag the pin to the right spot. A suggestion applies automatically once it clears an **engagement-tiered** support threshold (a brand-new contributor needs more corroboration than an established one), is capped at **2 km from the pin's original position** so it can't be walked across town, and can be optionally **GPS-corroborated** — your device checks whether you're within 75 m and sends only a yes/no boolean; **coordinates are never stored, correlated, or sold**, and GPS only *boosts* weight, never gates a good-faith fix.
- **Community signals:** rate perceived safety, bin condition, and how many bins are present — soft, crowd-sourced context shown alongside the confidence score.
- **Photos:** upload a photo (EXIF-stripped) and vote photos helpful/unhelpful. Photo uploads and photo votes are Turnstile-gated, just like voting.
- **Regions:** seed any configured region — `REGION=columbus` (default), `REGION=ohio` (statewide), `REGION=greater_ohio` (Ohio + bordering MI/IN/KY/WV/PA), any **two-letter state code** (`REGION=ca`, `REGION=tx`, …), or `REGION=usa` for the whole country. The 50 states + DC and the `usa` union are **derived from a vendored ZIP table** ([`pipeline/data/us_zips.csv`](pipeline/data/us_zips.csv)), not hand-listed — see [National coverage](#national-coverage). Add a custom metro or multi-state area by adding an entry to [`pipeline/regions.py`](pipeline/regions.py). Example: `REGION=greater_ohio bash scripts/seed.sh`.

## National coverage

OpenDrop ships the *capability* to cover all 50 states + DC, plus a **gentle, resumable overnight seeder** to fill it in. The national regions aren't hand-maintained: `state_regions()` and the `usa` union are derived at import time from the vendored ZIP table ([`pipeline/data/us_zips.csv`](pipeline/data/us_zips.csv)) — each state's bbox is computed from its own ZIP centroids (+pad), and ZIP-sweep scrapers (Salvation Army, USAgain) walk the full ~42k-ZIP list. The OSM ingest splits any region too large for one Overpass query into `OSM_TILE_DEGREES`-sized tiles and merges/dedupes them.

### Seeding the whole country (overnight)

Hitting five upstreams across ~42k ZIPs is a multi-hour job, so it's run by a **dedicated, restartable** script rather than the one-shot `scripts/seed.sh`:

```bash
# Runs in the api container; logs to stdout. Safe to start before bed.
bash scripts/seed_national.sh
```

- **Gentle by default.** Every upstream call goes through the polite HTTP client — inter-request pacing (`SCRAPER_REQUEST_DELAY_S`, default 0.5s), exponential backoff with jitter, and `Retry-After` / 429 / 5xx handling — so it stays within Nominatim's 1 req/s ToS and never hammers a source. Tune the pacing in [`.env`](.env.example).
- **Resumable.** Progress is checkpointed **per state** in the `seed_progress` table (migration 0008). If the run is interrupted (Ctrl-C, laptop sleep, a crash), just run it again — completed states are skipped, the state that was mid-flight is re-run, and the global dedup/promote finalize runs once at the end. Set `SEED_FORCE=1` to re-sweep everything from scratch.
- **One state at a time.** Seeding per-state (not one giant national query) bounds each request burst, scopes closure-detection correctly, and gives the checkpoint its natural granularity.

> Goodwill is **excluded** from the national seed (it's enrich-only — its ToS forbids storing its data). Running this hits live third-party APIs for hours; it's intended to be kicked off deliberately, not in CI.

## What's where

| Path | What |
|---|---|
| [`backend/`](backend/) | FastAPI app — REST API, Turnstile verification, IP-cooldown voting, confidence recompute |
| [`pipeline/`](pipeline/) | OSM ingest (tiled for large regions), dedup, scrapers (`salvation_army`, `planet_aid`, `usagain`, `wearable_collections` ingest; `goodwill` enrich-only), data-driven regions (+ vendored `data/us_zips.csv`), `seed` (one region), `seed_national` (gentle resumable all-states), promote |
| [`frontend/`](frontend/) | Vanilla-JS Leaflet single-page map (markers, clustering, popovers, submit, photo gallery, list view). Also `admin.html` — a token-gated operator console for the moderation queues (see [`docs/RUNBOOK.md`](docs/RUNBOOK.md) §5) |
| [`migrations/`](migrations/) | PostGIS schema as an ordered, append-only migration chain — `0001_init` (base schema), `0002` (confidence source-component fix), `0003` (consignment org_type), `0004` (community photos + image votes), `0005` (image-vote Turnstile), `0006` (pin corrections, community signals, engagement-tiered trust + consensus functions), `0007` (correction origin-anchor + retirement strict-dominance + per-attribute value bounds), `0008` (seed_progress — the resumable national-seed checkpoint table). Applied in order on first boot; shipped migrations are fixed forward with a new migration, never edited. |
| [`planning/`](planning/) | Architecture, data model, build sequence, validation |

## API (served at `/api`)

| Endpoint | Purpose |
|---|---|
| `GET /api/locations?bbox=w,s,e,n` | Map data — GeoJSON points, or server-side clusters when dense |
| `GET /api/locations/{id}` | Full detail for the popover (name, hours, confidence, sources) |
| `POST /api/locations/{id}/vote` | Confirm/deny (Turnstile + 24h IP cooldown; recomputes confidence) |
| `POST /api/locations` | Submit a new location — by address or by dropped pin lat/lon (geocoded, dedup-checked, auto-promoted) |
| `GET /api/locations/{id}/images` | Photos for a location (vouched only by default; `?include_low=true` for the full gallery) |
| `POST /api/locations/{id}/images` | Upload a photo + optional pin correction (Turnstile, EXIF-stripped, per-IP daily cap) |
| `POST /api/images/{id}/vote` | Vote a photo helpful/unhelpful (Turnstile; a correction photo auto-moves the pin at score ≥3) |
| `POST /api/locations/{id}/corrections` | Propose a corrected pin position (drag-to-fix; optional on-device GPS-corroborated boolean, ≤2 km from the location's origin) |
| `POST /api/corrections/{id}/vote` | Confirm/deny a proposed correction (Turnstile; applies the move once engagement-tiered support is met) |
| `POST /api/locations/{id}/attributes` | Rate community signals — perceived safety, bin condition, bin count (Turnstile; one rating per IP per attribute, per-IP daily cap) |
| `GET /api/reverse?lat=&lon=` | Reverse-geocode a dropped pin to a street address (Nominatim proxy + cache) — auto-fills the submit form |
| `GET /api/geosearch?q=` | Free-text place/address search (Nominatim proxy + cache) powering the search box |
| `GET /api/meta` | Counts + source attributions + Turnstile sitekey + **`coverage`** (bbox + center of all active data, so the map opens framed on what's actually loaded) |
| `GET /api/export` | Redistributable open-data dump (ODbL attribution embedded in payload) |
| `GET /api/health` | Liveness + DB check |

## Tests

```bash
# Fast — pure-logic tests, no services/network needed
python tests/test_dedup_logic.py
python tests/test_regions.py
pytest tests/test_regions_national.py tests/test_http_polite.py tests/test_osm_tiling.py

# Full suite — 76 tests (API + classify + dedup + regions + corrections/signals consensus
# + national regions / polite-HTTP backoff / OSM tiling / resumable-seed checkpoint).
# Spins up the DB and runs pytest in a throwaway container, because pytest lives in
# backend/requirements-dev.txt, not the slim runtime image. On Windows, run from Git Bash.
make test
```

> `make test` runs against a database named `opendrop`; the DB-backed tests insert and delete their own rows. Point `DATABASE_URL` at a throwaway DB if you don't want them touching a seeded one.

CI runs the same `ruff` lint + full `pytest` on every push and PR — see [`.github/workflows/ci.yml`](.github/workflows/ci.yml).

## License

- **Code:** [AGPL-3.0](LICENSE) — you may use, modify, and self-host freely, but if you run a modified OpenDrop **as a network service**, you must offer your source changes to its users. Chosen to keep the project community-owned and un-enclosable.
- **Exported dataset** (`/api/export`): **ODbL-1.0** (share-alike + attribution) — because OpenStreetMap is a stored source, the share-alike obligation is viral and applies to the whole dataset.
- **Community submissions:** released as **CC0** (public domain) so the open dataset stays freely reusable.

## Data, licensing & ethics

- **OpenStreetMap** data is **ODbL** — attributed on the map and embedded in `/api/export`.
- **The Salvation Army** (satruck.org) first-party locations are stored with attribution.
- **Planet Aid**, **USAgain**, and **Wearable Collections** drop-bin locations are first-party sources, stored with attribution.
- **Goodwill** is **enrich-only**: its ToS forbids storing/redistributing its data, so the scraper runs as a pattern demo and persists **nothing** (see [decision D1](research/FINDINGS.md)).
- **Google Places / Foursquare** are never stored — query-time enrichment only.
- No accounts; abuse is gated by Turnstile + per-IP cooldowns (good-enough, not adversarially hardened).

## Production deployment

A production overlay adds TLS, stops publishing internal ports, and turns on the secrets guard:

```bash
cp .env.example .env
# set: APP_ENV=prod, a strong POSTGRES_PASSWORD + IP_HASH_SALT (openssl rand -hex 24),
#      real Cloudflare TURNSTILE_SECRET/SITEKEY, and DOMAIN + ACME_EMAIL
docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile scheduler up -d --build
bash scripts/seed.sh
```

- **TLS:** Caddy auto-provisions/renews Let's Encrypt certs for `DOMAIN`, redirects HTTP→HTTPS, sets HSTS, and proxies to nginx. (Requires Docker Compose ≥ 2.24 for the `!reset` tag.)
- **Secrets guard:** with `APP_ENV=prod`, the API refuses to boot if `IP_HASH_SALT`, the DB password, or the Turnstile secret are still defaults.
- **Least-privilege DB:** run [`deploy/app_role.sql`](deploy/app_role.sql) and point the API's `DATABASE_URL` at `opendrop_app` (no DDL/DELETE); keep the owner role for migrations + the scheduler.
- **Freshness:** the `scheduler` profile re-syncs sources daily (`SYNC_INTERVAL_SECONDS`), which also runs closure-detection. Closure-detection is **circuit-broken** — it refuses to retire links when a run saw fewer than `RECONCILE_MIN_SEEN` (default 5) records or would retire more than `RECONCILE_MAX_FRACTION` (default 0.40) of a source's in-region links, so a truncated/blocked upstream response can't mass-delete a region.
- **Backups:** `bash scripts/backup.sh` (gzipped `pg_dump`) / `bash scripts/restore.sh <dump>`. Note: `docker compose down -v` **wipes the database volume** — back up first.

> Recommended VPS: Hetzner CX22 (2 vCPU / 4 GB) or a DigitalOcean 2 GB droplet.
