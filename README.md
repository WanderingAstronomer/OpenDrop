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

## License

- **Code:** [AGPL-3.0](LICENSE) — you may use, modify, and self-host freely, but if you run a modified OpenDrop **as a network service**, you must offer your source changes to its users. Chosen to keep the project community-owned and un-enclosable.
- **Exported dataset** (`/api/export`): **ODbL-1.0** (share-alike + attribution) — because OpenStreetMap is a stored source, the share-alike obligation is viral and applies to the whole dataset.
- **Community submissions:** released as **CC0** (public domain) so the open dataset stays freely reusable.

## Data, licensing & ethics

- **OpenStreetMap** data is **ODbL** — attributed on the map and embedded in `/api/export`.
- **The Salvation Army** (satruck.org) first-party locations are stored with attribution.
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
- **Freshness:** the `scheduler` profile re-syncs sources daily (`SYNC_INTERVAL_SECONDS`), which also runs closure-detection.
- **Backups:** `bash scripts/backup.sh` (gzipped `pg_dump`) / `bash scripts/restore.sh <dump>`. Note: `docker compose down -v` **wipes the database volume** — back up first.

> Recommended VPS: Hetzner CX22 (2 vCPU / 4 GB) or a DigitalOcean 2 GB droplet.
