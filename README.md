# OpenDrop

> Community-owned, open-data map of every clothing donation location in the United States — drop bins, thrift stores, mutual aid closets, church drives, seasonal collection points. Civic infrastructure, not a product.

OpenDrop seeds a map from **redistributable** first-party sources (OpenStreetMap + The Salvation Army), deduplicates them with a validated geo + fuzzy-name algorithm, and keeps the data fresh via **crowd validation** (confirm/deny votes) gated by Cloudflare Turnstile and per-IP cooldowns — no user accounts.

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

# 4. Seed real Ohio donation data (OSM + Salvation Army, deduped)
bash scripts/seed.sh

# 5. Open the map
#    -> http://localhost:8080
```

That's it. The map loads donation locations for the Columbus, OH metro. Click a pin for details and to **confirm / deny** it (the confidence score updates live). Use **＋ Add location** to submit a new spot.

To stop: `docker compose down` (add `-v` to also wipe the database volume).

## What's where

| Path | What |
|---|---|
| [`backend/`](backend/) | FastAPI app — REST API, Turnstile verification, IP-cooldown voting, confidence recompute |
| [`pipeline/`](pipeline/) | OSM ingest, dedup, scrapers (`salvation_army` ingest, `goodwill` enrich-only), seed, promote |
| [`frontend/`](frontend/) | Vanilla-JS Leaflet single-page map (markers, clustering, popovers, submit) |
| [`migrations/0001_init.sql`](migrations/0001_init.sql) | Full PostGIS schema (applied on first boot) |
| [`planning/`](planning/) | Architecture, data model, build sequence, validation |

## API (served at `/api`)

| Endpoint | Purpose |
|---|---|
| `GET /api/locations?bbox=w,s,e,n` | Map data — GeoJSON points, or server-side clusters when dense |
| `GET /api/locations/{id}` | Full detail for the popover (name, hours, confidence, sources) |
| `POST /api/locations/{id}/vote` | Confirm/deny (Turnstile + 24h IP cooldown; recomputes confidence) |
| `POST /api/locations` | Submit a new location (geocoded, dedup-checked, auto-promoted) |
| `GET /api/meta` | Counts + source attributions + Turnstile sitekey |
| `GET /api/export` | Redistributable open-data dump (ODbL attribution embedded in payload) |
| `GET /api/health` | Liveness + DB check |

## Tests

```bash
# Pure dedup-logic tests (no services needed)
PYTHONPATH=. python tests/test_dedup_logic.py

# Full API + pipeline tests (needs the DB up)
docker compose up -d db
docker compose run --rm api pytest -q
```

## Data, licensing & ethics

- **OpenStreetMap** data is **ODbL** — attributed on the map and embedded in `/api/export`.
- **The Salvation Army** (satruck.org) first-party locations are stored with attribution.
- **Goodwill** is **enrich-only**: its ToS forbids storing/redistributing its data, so the scraper runs as a pattern demo and persists **nothing** (see [decision D1](research/FINDINGS.md)).
- **Google Places / Foursquare** are never stored — query-time enrichment only.
- No accounts; abuse is gated by Turnstile + per-IP cooldowns (good-enough, not adversarially hardened).

> Status: local development instance. Production deployment (single VPS, e.g. Hetzner CX22) is configured but not deployed — that needs live credentials.
