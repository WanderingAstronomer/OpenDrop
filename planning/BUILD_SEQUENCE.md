# OpenDrop — Build Sequence (Phase 3 execution queue)

> Ordered, dependency-annotated task list. Phase 3 works top-to-bottom; tasks with satisfied dependencies may run in parallel. Each task lists **deps**, **deliverable**, and **done-when**. Mark `[x]` inline as completed. No task should require re-planning — if one does, that is a spec gap to fix in [ARCHITECTURE.md](ARCHITECTURE.md)/[DATA_MODEL.md](DATA_MODEL.md) first.

Legend: `[ ]` todo · `[x]` done · deps reference task IDs.

---

## Group A — Skeleton & infra (no upstream deps)

- [x] **A1 — Directory skeleton.** Create `backend/app/routers/`, `frontend/js/`, `pipeline/scrapers/`, `migrations/`, `scripts/`. Deps: none. Done-when: tree matches AGENTS.md convention.
- [x] **A2 — `docker-compose.yml` + `.env.example`.** Services `db` (postgis/postgis:17-3.5, pgdata volume, healthcheck, initdb mount), `api` (build ./backend, depends_on db healthy), `web` (nginx, static + /api proxy). All env vars from ARCHITECTURE §8. Deps: A1. Done-when: `docker compose config` validates.
- [x] **A3 — `backend/Dockerfile` + `pyproject.toml`.** Pin fastapi, uvicorn[standard], psycopg[binary,pool], pydantic v2, httpx, selectolax, python-dotenv, pytest. Deps: A1.
- [x] **A4 — `frontend/nginx.conf`.** Serve `/`, proxy `/api/` → `api:8000`, set `X-Forwarded-For`. Deps: A1.

## Group B — Database (the contract)

- [x] **B1 — `migrations/0001_init.sql`.** Verbatim from DATA_MODEL: extensions, 6 enums (in `0001`; the migration chain now spans `0001`–`0005` and adds a 7th enum, `image_status`, in `0004` — plus the `consignment` `org_type` value in `0003`), `sources` (+7 seed rows), `locations` (incl. `brand_key`, `house_number`, `state varchar(2)`), `location_sources`, `votes`, `pending_locations`, `scrape_log`, `normalize_name()`, `recompute_confidence()` (with deny-dominance override + `COUNT(*)>0` redist), `trg_after_vote`/`trg_after_source` (NULL-safe `last_verified_at`), `v_public_locations`. Plain `CREATE`s — applied once via the ledger, NOT self-idempotent. Deps: A1.
- [x] **B2 — `scripts/migrate.sh` (ledger-guarded).** Ensure `schema_migrations(version text PK, applied_at)`; apply each `migrations/NNNN_*.sql` whose `version` is absent, then record it (so re-runs are no-ops without `IF NOT EXISTS`). First-boot path: the same `0001_init.sql` is mounted into `/docker-entrypoint-initdb.d`. Deps: B1, A2.
- [x] **B3 — DB smoke test.** Bring up `db`, apply B1, assert all tables/enums/functions/triggers/view exist, `sources` has 7 rows, and a second `migrate.sh` run is a clean no-op. Deps: B1, B2, A2.

## Group C — Backend core

- [x] **C1 — `config.py` + `db.py`.** Pydantic `Settings` from env; async psycopg pool in FastAPI lifespan. Deps: A3, B1.
- [x] **C2 — `models.py`.** Pydantic v2 request/response models for every endpoint in ARCHITECTURE §4. Deps: A3.
- [x] **C3 — `security.py`.** `ip_hash(ip)`; Turnstile `verify(token, ip)` (real siteverify + **dev-mock**: test secret passes but empty token always fails); cooldown query helper. Deps: C1.
- [x] **C4 — `deps.py`.** FastAPI dependencies: resolve **trusted** client IP (read `X-Real-IP` set by nginx `$remote_addr`; ignore inbound `X-Forwarded-For`), `ip_hash`, `require_turnstile`. Deps: C3.
- [x] **C5 — `geocode.py`.** Nominatim client (`NOMINATIM_URL`, UA, structured query → lat/lon). Deps: C1.
- [x] **C6 — `main.py`.** App factory, CORS from env, lifespan pool, router mounting, `GET /api/health`. Deps: C1.

## Group E — Pipeline shared (needed by both API submit and jobs; precede D4/F*)

- [x] **E1 — `pipeline/common.py`.** DB writer/upsert helpers; `normalize_name` + `normalize_house_number` (mirror SQL); **brand canonicalization** map → `brand_key`; field-provenance writer (canonical columns only from ingest sources, by authority); `haversine`; shared with API for submit dedup. Deps: B1.
- [x] **E2 — `pipeline/dedup.py`.** Candidate gen `ST_DWithin(...,600)` over `status NOT IN ('merged','hidden')`; predicate = `brand_equal AND ((≤300 & name_sim≥0.4) OR (≤600 & name_sim≥0.4 & house_number_equal))` **OR** unbranded-bin path (`both brand_key NULL & same org_type∈{drop_bin,donation_center} & ≤25m`); `merge()` (canonical=max authority then oldest; field-provenance recompute; repoint sources; chain-compact `merged_into_id`; reset loser `source_count`/status). Port Phase-1 `dedup_sample.py`. Deps: E1.
- [x] **E3 — `pipeline/scrapers/base.py`.** `NormalizedRecord`, `BaseScraper`, `loader.load(scraper, region)` honoring `sources.storage_policy` (ingest persists; enrich_only → `scrape_log` only, 0 `location_sources`). Deps: E1, E2.

## Group D — API endpoints (deps on C*, E2)

- [x] **D1 — `GET /api/locations`.** bbox validate; adaptive points (≤POINT_CAP) vs PostGIS grid clusters; GeoJSON. Deps: C1, C2, C6.
- [x] **D2 — `GET /api/locations/{id}`.** Detail + joined sources; 404 (+canonical id for merged). Deps: C1, C2.
- [x] **D3 — `POST /api/locations/{id}/vote`.** Turnstile → trusted IP (X-Real-IP, §6) → `pg_advisory_xact_lock` → cooldown(429) → insert vote → trigger recompute → return updated confidence/status. Deps: C3, C4, B1.
- [x] **D4 — `POST /api/locations`.** Turnstile → geocode → dedup-check (E2) → insert `pending_locations`; **on geocoded+non-dup, call promotion (D7) inline** → return `location_id`/`duplicate_of`. Deps: C4, C5, E2, D7.
- [x] **D5 — `GET /api/meta` + `GET /api/export`.** meta counts + **ingest-only contributing** sources + sitekey; export streams `v_public_locations` with **in-payload** `attribution`/`license` members. Deps: C1, C2.
- [x] **D6 — Backend tests (pytest, ASGI).** Endpoint shapes; vote raises confidence on a fresh row; **single-source 4 denies → pending AND multi-source 5 denies (override) → pending**; concurrent double-vote → one row (advisory lock); forged left-most XFF does NOT change ip_hash; cooldown → 429; missing token → 403 (dev mock); `export` excludes enrich-only/non-redistributable AND carries in-payload attribution. Deps: D1–D5.
- [x] **D7 — `pipeline/promote.py` + inline promotion.** Move `pending_locations(awaiting)` → `locations` (crowd source, conf≈20, status pending) when geocoded + non-dup; set `status='promoted'`/`promoted_location_id`; duplicate → `status='duplicate'`; no-geom → stays awaiting. Runnable as `python -m pipeline.promote` and callable from D4. Test: promoted row hidden until 1 confirm lifts ≥25. Deps: E2, C5. **(closes Phase-2 review blocker)**

## Group F — Concrete pipeline jobs

- [x] **F1 — `pipeline/osm_ingest.py`.** Overpass fetch (region bbox) → normalize → loader upsert (`osm`). Reuse Phase-1 query. Deps: E1, E2, E3.
- [x] **F2 — `pipeline/scrapers/salvation_army.py`** (ingest). ZIP-sweep satruck API; dedupe on LocationGUID; parse Hours/TypeName. Deps: E3. *(See Group I for the three additional ingest scrapers — Planet Aid, USAgain, Wearable Collections — that shipped alongside this one.)*
- [x] **F3 — `pipeline/scrapers/goodwill.py`** (enrich_only). Nonce harvest + tiled `gwlf_get_locations` cats=1; full path, **persists nothing**. Deps: E3.
- [x] **F4 — `pipeline/seed.py`.** Orchestrate, for the selected `REGION`, OSM + Salvation Army + Planet Aid + USAgain + Wearable Collections (all ingest) + Goodwill (enrich) + `dedup.run()` + `promote.run()`. Each live scraper after OSM is wrapped in a non-fatal `_try` so an unreachable endpoint never aborts the seed. **Deterministic fallback:** if a live endpoint (Overpass/satruck) is unreachable at seed time, fall back to the committed Phase-1 fixture [`research/data/osm_columbus.json`](../research/data/osm_columbus.json) for OSM so `docker compose up`+seed always yields a non-empty map. Log expected vs actual counts. Deps: F1, F2, F3, E2.
- [x] **F5 — Pipeline tests.** Dedup merges the Phase-1 dirty Goodwill pair at 0 FP (`merged_into_id` set, chain-compacted); **unbranded-bin fixture** merges only within ≤25 m; **field-provenance** test: a canonical column never holds an enrich_only value; enrich_only run writes `scrape_log` `records_upserted=0` + 0 `location_sources`. Deps: E2, F2, F3.

## Group G — Frontend

- [x] **G1 — `index.html` + `css`.** Full-viewport map, "＋ Add location" button, toast container. Deps: none.
- [x] **G2 — `config.js` + `api.js`.** Endpoint wrappers; bbox/zoom serialization; loads `/api/meta` for sitekey + buckets. Deps: ARCHITECTURE §4.
- [x] **G3 — `map.js`.** Leaflet 1.9.4 init, OSM tiles + ODbL attribution control (augmented from meta), debounced move/zoom → load. Deps: G2.
- [x] **G4 — `markers.js`.** Render points via Leaflet.markercluster (pinned **1.5.3**); render server clusters as count bubbles when `mode==='clusters'`; bucket colors. Deps: G3, D1.
- [x] **G5 — `confidence.js` + `popover.js`.** Pin click → detail → name/type/address/hours/confidence badge + vote buttons. Deps: G2, D2.
- [x] **G6 — `vote.js`.** Inline Turnstile widget in popover → submit vote → update badge; handle 429/403/404. Deps: G5, D3.
- [x] **G7 — `submit.js`.** Add-location form (name/address/org_type) + inline Turnstile → POST → toast (added/duplicate). Deps: G2, D4.

## Group H — Integration, seed, docs

- [x] **H1 — `scripts/seed.sh`.** `docker compose run --rm api python -m pipeline.seed` against `SEED_REGION_BBOX`. Deps: F4, A2.
- [x] **H2 — End-to-end bring-up.** `docker compose up` → migrate → `bash scripts/seed.sh` → open **http://localhost:8080** → map renders real Ohio data (expect ≳40 active locations from OSM+SA after dedup; `drop_bin`/`mutual_aid`/`church_drive` may legitimately be sparse/empty in the Columbus seed — **not** a defect), vote buttons work, **no console errors**. (= Phase 3 end condition.) Deps: all.
- [x] **H3 — `README.md`.** Setup-to-running-map in <10 steps; env, compose up, seed, open browser. Deps: H2.

## Group I — Post-Phase-4 additions (shipped & validated)

> Work landed after the original Phase-3 queue closed. All items below are **done**; listed here so the build record stays complete.

- [x] **I1 — Three additional ingest scrapers.** `pipeline/scrapers/planet_aid.py`, `pipeline/scrapers/usagain.py`, `pipeline/scrapers/wearable_collections.py` — all `storage_policy=ingest`, all wired into `pipeline/seed.py` (via the non-fatal `_try` wrapper) alongside OSM + Salvation Army + Goodwill. Coverage caveats: USAgain has **no Ohio** coverage; Wearable Collections is **NYC-only** — both are still seeded so non-Ohio regions populate. Deps: E3.
- [x] **I2 — `pipeline/regions.py` (region module + `greater_ohio`).** `REGIONS` registry with `columbus` (DEFAULT), `ohio` (statewide), and the new `greater_ohio` (multi-state: Ohio + bordering MI/IN/KY/WV/PA; `bbox=(36.50,-88.20,44.00,-74.70)` south/west/north/east, `center=(40.20,-81.50)`, `radius_mi=300`, plus a cross-state ZIP sweep list `GREATER_OHIO_ZIPS`). `Region` dataclass fields: `name, bbox, center, zips, radius_mi`. Selected via the `REGION` env var; `get_region()` falls back to `columbus` on an unknown name. `SEED_REGION_BBOX` still overrides the **columbus** bbox only. Deps: E1.
- [x] **I3 — Consignment migration (`migrations/0003_add_consignment.sql`).** Adds the `consignment` value to the `org_type` enum **after** `thrift_store`; `backend/app/models.py` `OrgType` literal updated to include it. Deps: B1.
- [x] **I4 — Community photos feature (`migrations/0004_images.sql` + `backend/app/routers/images.py` + `backend/app/imageproc.py` + `frontend/js/photos.js`).** Migration 0004 adds the `image_status` enum (`pending`/`visible`/`hidden`), the `location_images` table (path, mime, `submitter_ip_hash`, `turnstile_hash`, `suggested_lat`/`suggested_lon` for pin corrections, `upvotes`/`downvotes`/`score`, `status`, `applied`) and `image_votes` table (`image_id`, `ip_hash`, `helpful`, `UNIQUE(image_id, ip_hash)`), plus `recompute_image()` and the `trg_after_image_vote` trigger that auto-moves the canonical location pin to the suggested coords once a correction photo reaches `score >= 3`. API: `GET /api/locations/{id}/images` (gallery; `?include_low=true` shows pending/hidden), `POST /api/locations/{id}/images` (upload + optional pin correction; Turnstile + EXIF-strip via `imageproc` + per-IP daily cap), `POST /api/images/{id}/vote` (advisory-lock; auto-applies the pin correction at `score >= 3`). New env (`backend/app/config.py`): `MEDIA_DIR` (`/app/media`), `IMAGE_MAX_BYTES` (`6000000`), `IMAGE_UPLOADS_PER_IP_PER_DAY` (`8`). Frontend `photos.js` does gallery/upload/vote/click-map pin correction. Deps: B1, C-group.
- [x] **I5 — Accessible list view (`frontend/js/list.js`).** Keyboard/screen-reader-friendly list view of locations with a category filter, alongside the map. Deps: G-group.
- [x] **I6 — Geosearch (`backend/app/routers/meta.py` + `frontend/js/search.js`).** `GET /api/geosearch?q=` Nominatim proxy + cache backing the search box. Deps: C-group, G-group.
- [x] **I7 — Reconciliation circuit breaker (`pipeline/scrapers/base.py` `_reconcile`).** Closure-detection now **refuses to retire** a source's in-region links when a run saw fewer than `RECONCILE_MIN_SEEN` records (default 5), OR when retiring would remove more than `RECONCILE_MAX_FRACTION` (default 0.40) of that source's **current** in-region links (counted before deleting, same txn). Both env-overridable (read via `os.environ`). Still skipped entirely if a run had any per-record errors; region-scoped to the bbox. Deps: E3.
- [x] **I8 — Turnstile on image votes (`migrations/0005_image_vote_turnstile.sql`).** Adds `image_votes.turnstile_hash`; `backend/app/models.py` `ImageVoteIn.turnstile_token` + the `images.py` `vote_image` gate + a Turnstile widget in the frontend gallery — mirroring the location-vote and photo-upload Turnstile paths. Deps: I4.

---

## Critical path

`A1 → B1 → E1 → E2 → E3 → F1/F2/F3 → F4 → H1 → H2`. Backend (C/D) and frontend (G) parallelize against the DB contract (B1) and API spec once E2 exists (D4 needs dedup + D7 promotion). Seed (F4) gates the end-to-end demo (H2).

## Definition of done (Phase 3)

`docker compose up` then `bash scripts/seed.sh` yields a browser map (http://localhost:8080) of **real Ohio donation locations**, with functional confirm/deny buttons (confidence updates live; a multi-source location is retire-able via the deny-dominance override), a working submission flow (geocoded submissions appear after promotion + a confirm), ODbL + source attribution visible **on the map and in `/api/export`'s payload**, and **zero console errors**. No `enrich_only` (Goodwill) row appears in `/api/export`.
