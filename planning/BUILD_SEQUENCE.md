# OpenDrop — Build Sequence (Phase 3 execution queue)

> Ordered, dependency-annotated task list. Phase 3 works top-to-bottom; tasks with satisfied dependencies may run in parallel. Each task lists **deps**, **deliverable**, and **done-when**. Mark `[x]` inline as completed. No task should require re-planning — if one does, that is a spec gap to fix in [ARCHITECTURE.md](ARCHITECTURE.md)/[DATA_MODEL.md](DATA_MODEL.md) first.

Legend: `[ ]` todo · `[x]` done · deps reference task IDs.

---

## Group A — Skeleton & infra (no upstream deps)

- [ ] **A1 — Directory skeleton.** Create `backend/app/routers/`, `frontend/js/`, `pipeline/scrapers/`, `migrations/`, `scripts/`. Deps: none. Done-when: tree matches AGENTS.md convention.
- [ ] **A2 — `docker-compose.yml` + `.env.example`.** Services `db` (postgis/postgis:17-3.5, pgdata volume, healthcheck, initdb mount), `api` (build ./backend, depends_on db healthy), `web` (nginx, static + /api proxy). All env vars from ARCHITECTURE §8. Deps: A1. Done-when: `docker compose config` validates.
- [ ] **A3 — `backend/Dockerfile` + `pyproject.toml`.** Pin fastapi, uvicorn[standard], psycopg[binary,pool], pydantic v2, httpx, selectolax, python-dotenv, pytest. Deps: A1.
- [ ] **A4 — `frontend/nginx.conf`.** Serve `/`, proxy `/api/` → `api:8000`, set `X-Forwarded-For`. Deps: A1.

## Group B — Database (the contract)

- [ ] **B1 — `migrations/0001_init.sql`.** Verbatim from DATA_MODEL: extensions, 6 enums, `sources` (+7 seed rows), `locations`, `location_sources`, `votes`, `pending_locations`, `scrape_log`, `normalize_name()`, `recompute_confidence()`, `trg_after_vote`/`trg_after_source` triggers, `v_public_locations`. Deps: A1.
- [ ] **B2 — `scripts/migrate.sh`.** Idempotent `psql -f` apply (guards via `IF NOT EXISTS` / migration ledger). Wire same SQL to the db initdb mount. Deps: B1, A2.
- [ ] **B3 — DB smoke test.** Bring up `db`, apply B1, assert all tables/enums/functions/triggers/view exist and `sources` has 7 rows. Deps: B1, B2, A2.

## Group C — Backend core

- [ ] **C1 — `config.py` + `db.py`.** Pydantic `Settings` from env; async psycopg pool in FastAPI lifespan. Deps: A3, B1.
- [ ] **C2 — `models.py`.** Pydantic v2 request/response models for every endpoint in ARCHITECTURE §4. Deps: A3.
- [ ] **C3 — `security.py`.** `ip_hash(ip)`; Turnstile `verify(token, ip)` (real siteverify + **dev-mock**: test secret passes but empty token always fails); cooldown query helper. Deps: C1.
- [ ] **C4 — `deps.py`.** FastAPI dependencies: resolve client IP (X-Forwarded-For left-most), `ip_hash`, `require_turnstile`. Deps: C3.
- [ ] **C5 — `geocode.py`.** Nominatim client (`NOMINATIM_URL`, UA, structured query → lat/lon). Deps: C1.
- [ ] **C6 — `main.py`.** App factory, CORS from env, lifespan pool, router mounting, `GET /api/health`. Deps: C1.

## Group E — Pipeline shared (needed by both API submit and jobs; precede D4/F*)

- [ ] **E1 — `pipeline/common.py`.** DB writer/upsert helpers; `normalize_name` (mirror of SQL); **brand canonicalization** map; `haversine`; shared with API for submit dedup. Deps: B1.
- [ ] **E2 — `pipeline/dedup.py`.** Candidate gen via `ST_DWithin(...,600)`; predicate `brand_equal AND ((≤300 & name_sim≥0.4) OR (≤600 & street#=))`; `merge()` (pick canonical, repoint sources, set merged). Port Phase-1 `dedup_sample.py`. Deps: E1.
- [ ] **E3 — `pipeline/scrapers/base.py`.** `NormalizedRecord`, `BaseScraper`, `loader.load(scraper, region)` honoring `sources.storage_policy` (ingest persists; enrich_only logs only). Deps: E1, E2.

## Group D — API endpoints (deps on C*, E2)

- [ ] **D1 — `GET /api/locations`.** bbox validate; adaptive points (≤POINT_CAP) vs PostGIS grid clusters; GeoJSON. Deps: C1, C2, C6.
- [ ] **D2 — `GET /api/locations/{id}`.** Detail + joined sources; 404 (+canonical id for merged). Deps: C1, C2.
- [ ] **D3 — `POST /api/locations/{id}/vote`.** Turnstile → cooldown(429) → insert vote → trigger recompute → return updated confidence/status. Deps: C3, C4, B1.
- [ ] **D4 — `POST /api/locations`.** Turnstile → geocode → dedup-check (E2) → insert `pending_locations` (awaiting/duplicate/no-geom). Deps: C4, C5, E2.
- [ ] **D5 — `GET /api/meta` + `GET /api/export`.** meta counts/sources/sitekey; export streams `v_public_locations` only. Deps: C1, C2.
- [ ] **D6 — Backend tests (pytest, ASGI).** Endpoint shapes; vote raises confidence; 5 denies → `pending`; cooldown → 429; missing token → 403 (dev mock); `export` excludes enrich-only/non-redistributable. Deps: D1–D5.

## Group F — Concrete pipeline jobs

- [ ] **F1 — `pipeline/osm_ingest.py`.** Overpass fetch (region bbox) → normalize → loader upsert (`osm`). Reuse Phase-1 query. Deps: E1, E2, E3.
- [ ] **F2 — `pipeline/scrapers/salvation_army.py`** (ingest). ZIP-sweep satruck API; dedupe on LocationGUID; parse Hours/TypeName. Deps: E3.
- [ ] **F3 — `pipeline/scrapers/goodwill.py`** (enrich_only). Nonce harvest + tiled `gwlf_get_locations` cats=1; full path, **persists nothing**. Deps: E3.
- [ ] **F4 — `pipeline/seed.py`.** Orchestrate OSM(Ohio) + Salvation Army(Ohio) + Goodwill(Ohio, enrich) + `dedup.run()`. Deps: F1, F2, F3, E2.
- [ ] **F5 — Pipeline tests.** Dedup merges the Phase-1 dirty pair at 0 FP; `merged_into_id` set; enrich_only writes `scrape_log` with `records_upserted=0` and 0 `location_sources` rows. Deps: E2, F2, F3.

## Group G — Frontend

- [ ] **G1 — `index.html` + `css`.** Full-viewport map, "＋ Add location" button, toast container. Deps: none.
- [ ] **G2 — `config.js` + `api.js`.** Endpoint wrappers; bbox/zoom serialization; loads `/api/meta` for sitekey + buckets. Deps: ARCHITECTURE §4.
- [ ] **G3 — `map.js`.** Leaflet 1.9.4 init, OSM tiles + ODbL attribution control (augmented from meta), debounced move/zoom → load. Deps: G2.
- [ ] **G4 — `markers.js`.** Render points via Leaflet.markercluster; render server clusters as count bubbles when `mode==='clusters'`; bucket colors. Deps: G3, D1.
- [ ] **G5 — `confidence.js` + `popover.js`.** Pin click → detail → name/type/address/hours/confidence badge + vote buttons. Deps: G2, D2.
- [ ] **G6 — `vote.js`.** Inline Turnstile widget in popover → submit vote → update badge; handle 429/403/404. Deps: G5, D3.
- [ ] **G7 — `submit.js`.** Add-location form (name/address/org_type) + inline Turnstile → POST → toast (added/duplicate). Deps: G2, D4.

## Group H — Integration, seed, docs

- [ ] **H1 — `scripts/seed.sh`.** `docker compose run --rm api python -m pipeline.seed` against `SEED_REGION_BBOX`. Deps: F4, A2.
- [ ] **H2 — End-to-end bring-up.** `docker compose up` → migrate → `bash scripts/seed.sh` → map renders real Ohio data, vote buttons work, **no console errors**. (= Phase 3 end condition.) Deps: all.
- [ ] **H3 — `README.md`.** Setup-to-running-map in <10 steps; env, compose up, seed, open browser. Deps: H2.

---

## Critical path

`A1 → B1 → E1 → E2 → E3 → F1/F2/F3 → F4 → H1 → H2`. Backend (C/D) and frontend (G) parallelize against the DB contract (B1) and API spec once E2 exists (D4 needs dedup). Seed (F4) gates the end-to-end demo (H2).

## Definition of done (Phase 3)

`docker compose up` then `bash scripts/seed.sh` yields a browser map of **real Ohio donation locations**, with functional confirm/deny buttons (confidence updates live), a working submission flow, ODbL + source attribution visible, and **zero console errors**. No `enrich_only` (Goodwill) row appears in `/api/export`.
