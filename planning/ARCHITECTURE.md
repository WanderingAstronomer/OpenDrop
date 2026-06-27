# OpenDrop — Architecture

> Complete build specification. Phase 3 executes this without further planning. Grounded in [research/FINDINGS.md](../research/FINDINGS.md); schema details live in [DATA_MODEL.md](DATA_MODEL.md); task order in [BUILD_SEQUENCE.md](BUILD_SEQUENCE.md).

---

## 1. System overview

```
                          ┌──────────────────────────────────────────────┐
                          │                Single VPS                     │
                          │           (Docker Compose stack)              │
   Browser                │                                               │
  ┌────────┐   HTTPS      │   ┌─────────┐   /api/*    ┌───────────────┐   │
  │ Leaflet│──────────────┼──▶│  web    │────────────▶│  api          │   │
  │  SPA   │   static +   │   │ (nginx) │  reverse    │ (FastAPI/     │   │
  │ +Turn- │◀─────────────┼───│ static  │  proxy      │  uvicorn)     │   │
  │ stile  │   /api JSON  │   └─────────┘             └──────┬────────┘   │
  └────────┘              │                                  │ SQL        │
       ▲                  │                           ┌──────▼────────┐   │
       │ Turnstile        │                           │ db            │   │
       │ challenge        │                           │ Postgres 17 + │   │
  ┌────┴─────┐            │                           │ PostGIS 3.6   │   │
  │Cloudflare│            │                           └──────▲────────┘   │
  │Turnstile │            │                                  │            │
  └──────────┘            │   ┌──────────────────────────────┴────────┐   │
                          │   │ pipeline (one-shot / cron jobs)        │   │
   siteverify  ◀──────────┼───│  osm_ingest · scrapers/* · dedup       │   │
   (api → CF)             │   └────────────────────────────────────────┘   │
                          └──────────────────────────────────────────────┘
                                      ▲                 ▲
                                      │ Overpass        │ org locators (satruck,
                                      │ (batch only)    │ planetaid, usagain, goodwill*)
                              overpass-api.de      *goodwill = enrich-only, never stored
```

**Data flow:** Pipeline jobs pull from Overpass + org locators → normalize → dedup → write canonical `locations`/`location_sources` in PostGIS. The API serves the map **only from PostGIS** (never proxies Overpass live — FINDINGS Finding 5 ⚠). Browsers read the map and write votes/submissions, gated by Turnstile + IP-hash cooldown.

---

## 2. Backend: **Python 3.12 + FastAPI** (chosen; defended)

The directive requires picking Node/Express **or** Python/FastAPI and defending it.

**Choice: Python + FastAPI**, for the whole backend *and* pipeline (one language, shared models).

| Reason | Detail |
|---|---|
| **The pipeline is the hard part, and it's data-processing** | OSM normalization, fuzzy/geo dedup, scraping. Phase 1's dedup harness is *already* Python (`difflib`, `math.haversine`, optional `rapidfuzz`). Python's geospatial/scraping ecosystem (shapely, httpx, lxml/selectolax) is materially stronger than Node's. |
| **One language end-to-end** | API and pipeline share the same normalization, confidence constants, and DB models. No serialization seam, no duplicated dedup logic. |
| **PostGIS pairs cleanly** | `psycopg` (v3) gives first-class async + raw SQL; we lean on PostGIS functions (ST_DWithin, ST_ClusterDBSCAN) rather than an ORM's geo abstractions. |
| **FastAPI is boring-proven** | Mature, typed (Pydantic v2) request/response validation, async, trivial OpenAPI. Fits "boring, proven technology." |
| **Turnstile + rate-limit are simple middleware** | siteverify is one `httpx.post`; cooldown is one indexed query. No framework gymnastics. |

**Libraries (pinned in Phase 3):** `fastapi`, `uvicorn[standard]`, `psycopg[binary,pool]` (v3), `pydantic` v2, `httpx`, `selectolax` (HTML parse for USAgain/GreenDrop-style), `python-dotenv`. No ORM (raw parameterized SQL against the schema). Tests: `pytest` + `httpx` ASGI transport.

**App layout:**
```
backend/
  app/
    main.py          # FastAPI app, CORS, router mounting, lifespan (db pool)
    config.py        # env-driven settings (Pydantic BaseSettings)
    db.py            # async psycopg pool + helpers
    models.py        # Pydantic request/response schemas
    deps.py          # dependencies: turnstile verify, ip_hash, cooldown
    security.py      # ip hashing, turnstile client, rate-limit logic
    geocode.py       # Nominatim client (submission geocoding)
    routers/
      locations.py   # GET list/detail, POST submit
      votes.py       # POST vote
      meta.py        # GET meta/health/stats
  tests/
  pyproject.toml
  Dockerfile
```

---

## 3. Database

PostgreSQL 17 + PostGIS 3.6 (decision D3). Full schema in [DATA_MODEL.md](DATA_MODEL.md): tables `sources`, `locations`, `location_sources`, `votes`, `pending_locations`, `scrape_log`; `recompute_confidence()` + vote/source triggers; `v_public_locations` redistributable view. Applied via `migrations/0001_init.sql`. Distance math uses `geography` casts so all thresholds are in meters.

---

## 4. API specification

Base path `/api`. JSON in/out. CORS limited to `CORS_ORIGINS`. All errors use a uniform body `{ "error": { "code": "<machine_code>", "message": "<human>" } }`. Timestamps ISO-8601 UTC.

### 4.1 `GET /api/locations` — map data (bbox query, adaptive points/clusters)

Query params:
| param | type | required | notes |
|---|---|---|---|
| `bbox` | `west,south,east,north` (lon/lat) | **yes** | validated; rejects > ~world span |
| `types` | csv of `org_type` | no | filter |
| `min_confidence` | number 0–100 | no | default 0 |
| `cluster` | `auto`\|`on`\|`off` | no | default `auto` |

Behavior: counts `active`, redistributable-agnostic (all active shown) locations in bbox. If `cluster=off`, or (`auto` and count ≤ `POINT_CAP=2000`) → **points mode**. Else → **cluster mode** (PostGIS `ST_SnapToGrid` aggregation sized to bbox span, ≤ `CLUSTER_CAP=400` cells).

Points response (GeoJSON, Leaflet-native):
```json
{ "mode": "points",
  "type": "FeatureCollection",
  "features": [
    { "type":"Feature",
      "geometry": {"type":"Point","coordinates":[-82.99,39.96]},
      "properties": {"id":123,"name":"Goodwill - Dublin","org_type":"charity_store",
                     "confidence":62.0,"bucket":"medium"} }
  ] }
```
Cluster response:
```json
{ "mode":"clusters",
  "clusters":[ {"lon":-82.9,"lat":39.9,"count":48,"avg_confidence":54.1} ] }
```
Errors: `400 bad_bbox`.

### 4.2 `GET /api/locations/{id}` — full detail (popover)

```json
{ "id":123, "name":"Goodwill - Dublin", "org_type":"charity_store", "org_name":"Goodwill",
  "lat":39.96, "lon":-82.99,
  "address":{"line":"6625 Dublin Center Dr","city":"Dublin","state":"OH","postal_code":"43017"},
  "hours":{"mon":[["09:00","21:00"]], "always":false}, "hours_raw":"Mon-Sat 9-9",
  "accepted_items":["clothing","shoes","household"], "phone":null, "website":null,
  "confidence":62.0, "bucket":"medium", "status":"active",
  "upvotes":4, "denies":1, "last_verified_at":"2026-06-20T12:00:00Z",
  "sources":[{"code":"osm","display_name":"OpenStreetMap","attribution":"© OpenStreetMap contributors (ODbL)"},
             {"code":"salvation_army","display_name":"The Salvation Army"}] }
```
Errors: `404 not_found`. (`merged` rows return `404` with `Location` of canonical id in body.)

### 4.3 `POST /api/locations/{id}/vote` — crowd vote

Request: `{ "vote":"confirm"|"deny", "turnstile_token":"<token>" }`
Pipeline (single transaction): verify Turnstile → derive `ip_hash` → **cooldown check** (any vote this `(location_id, ip_hash)` in 24h?) → insert `votes` row (trigger recomputes confidence + status) → return:
```json
{ "id":123, "confidence":67.0, "bucket":"medium", "status":"active", "upvotes":5, "denies":1 }
```
Errors: `400 bad_request` (missing/invalid vote), `403 turnstile_failed`, `404 not_found`, `429 cooldown_active` (includes `retry_after` seconds).

### 4.4 `POST /api/locations` — submit new location

Request: `{ "name":"...", "org_type":"drop_bin", "address":{"line":"...","city":"...","state":"OH","postal_code":"..."}, "turnstile_token":"<token>" }`
Pipeline: verify Turnstile → derive `ip_hash` → **geocode** address (Nominatim) → **dedup-check** against `locations` (same predicate as the batch dedup) → insert `pending_locations`:
- geocoded + no dupe → `status=awaiting` (eligible for promotion).
- dupe found → `status=duplicate`, `dupe_candidate_id` set; response surfaces the existing location.
- geocode failed → `status=awaiting`, `geom=NULL` (flagged for manual review).
```json
{ "pending_id":55, "status":"awaiting", "geocoded":true,
  "duplicate_of": null }
```
Errors: `400 bad_request`, `403 turnstile_failed`, `422 geocode_failed` (still records the submission), `429 submit_cooldown` (per-IP submit throttle, see §6).

### 4.5 `GET /api/meta` — attribution + stats (powers the map's attribution control)

```json
{ "counts":{"active":1234,"pending":56,"by_type":{"charity_store":...}},
  "sources":[{"code":"osm","attribution":"© OpenStreetMap contributors (ODbL)","license":"ODbL-1.0"}, ...],
  "turnstile_sitekey":"1x00000000000000000000AA",
  "confidence_buckets":{"high":70,"medium":40,"low":25} }
```

### 4.6 `GET /api/export` — redistributable open-data dump

Reads `v_public_locations` only (active + redistributable). Streams GeoJSON. Carries the aggregate attribution header. Guarantees no `enrich_only` data leaks (D1/D2). Optional `?state=OH`.

### 4.7 `GET /api/health` — liveness (DB ping). `200 {"status":"ok","db":true}`.

---

## 5. Frontend

Single full-viewport page. **Zero UI framework** beyond Leaflet + Turnstile (directive). Vanilla ES modules, served static (no build step required; an optional esbuild bundle is allowed but not needed).

**Dependencies (vendored/CDN-pinned):** Leaflet **1.9.4**, Leaflet.markercluster 1.5.x, Cloudflare Turnstile script (`challenges.cloudflare.com/turnstile/v0/api.js`).

**Component map** (`frontend/js/`):
| module | responsibility |
|---|---|
| `config.js` | API base, default view, Turnstile sitekey (from `/api/meta`), bucket colors |
| `api.js` | fetch wrappers for all endpoints; bbox/zoom serialization |
| `map.js` | Leaflet init, base tiles (OSM), attribution control, move/zoom (debounced) handler |
| `markers.js` | render points via markercluster **or** server clusters (when `mode==='clusters'`), bucket-colored pins |
| `popover.js` | on pin click → `GET /{id}` → render name/type/address/hours/confidence badge + vote UI |
| `vote.js` | renders inline **Turnstile widget** in popover, submits vote, updates badge; handles 429/403 |
| `submit.js` | "＋ Add location" panel: name/address/org_type form + Turnstile → `POST /locations` → toast |
| `confidence.js` | confidence → bucket → color/label helper (high≥70 green / medium≥40 amber / low≥25 red) |
| `toast.js` | minimal notifications |

**Clustering strategy:** below the point cap the server returns raw points and the client uses Leaflet.markercluster (smooth spiderfy at street level — "individual pins at street level"). When the server returns `mode:'clusters'` (zoomed out / dense), the client draws lightweight count bubbles ("cluster markers at low zoom"). The boundary is the server's `POINT_CAP`, so the client never has to hold the whole US in memory.

**Turnstile placement:** rendered **inline in the popover before vote submission** and inline in the submit panel (directive). The widget yields a single-use token passed to the API; the API re-verifies server-side (never trust the client).

**Map base tiles:** OpenStreetMap standard tiles with the required `© OpenStreetMap contributors` attribution; the attribution control is augmented from `/api/meta` sources (ODbL + each org). Note polite tile-usage; a self-hosted/tiles-provider swap is a documented later step, not a v1 blocker.

---

## 6. Crowd validation subsystem

- **Vote write** (§4.3): Turnstile verify → ip_hash → 24h cooldown → append `votes` → trigger `recompute_confidence` (DATA_MODEL §7). Confidence/status update is atomic with the vote.
- **IP cooldown:** `ip_hash = sha256(IP_HASH_SALT || client_ip)`. Client IP resolved from `X-Forwarded-For` left-most (nginx sets it) falling back to peer. 24h rolling window per `(location_id, ip_hash)`. Submit endpoint has its own coarser throttle: max `SUBMIT_PER_IP_PER_DAY=10`.
- **Turnstile integration points:** (1) vote popover, (2) submission panel. Server verifies via `POST https://challenges.cloudflare.com/turnstile/v0/siteverify` with `secret`, `response`, `remoteip`. Tokens are single-use, 300 s TTL (FINDINGS Finding 5). **Dev mock mode:** when `TURNSTILE_SECRET` is Cloudflare's test secret `1x0000000000000000000000000000000AA` (always-passes) the server still **requires a non-empty token** and rejects empty/missing ones — satisfying Phase 4 step 3 ("blocks submission without a valid token in dev mock mode"). Test sitekey `1x00000000000000000000AA` renders a passing widget locally.
- **Abuse resistance (good-enough, per directive):** Turnstile blocks scripted floods; IP-hash cooldown blocks repeat voting; denies weighted heavier than confirms so a location can be community-retired but a single actor can't nuke it instantly (needs distinct IP-hashes). Not adversarially hardened (no Sybil-proofing) — explicitly out of scope for v1.

---

## 7. Data pipeline

All jobs are Python modules run via `python -m pipeline.<job>` (one-shot or cron), writing to PostGIS. Each run records a `scrape_log` row.

### 7.1 OSM ingest — `pipeline/osm_ingest.py`
- Query Overpass (`OVERPASS_URL`, default overpass-api.de) for a region bbox: `shop=charity`, `shop=second_hand`, `amenity=recycling` + `recycling:clothes`/`recycling:shoes`. `[out:json][timeout:90]; ... out center tags;` (proven in Phase 1). Descriptive User-Agent.
- Normalize each element → canonical record (map tags → org_type, name, address, hours via `opening_hours`/`collection_times` when present, brand→org_name). `out center` gives a point for ways.
- **Upsert** keyed on `location_sources(source_code='osm', source_ref='<type>/<id>')`. New ref → run dedup-match against existing canonical; attach to match or create a new `locations` row + `location_sources` row. Existing ref → update `last_seen_at`/payload.
- Batch-only against the public endpoint (Finding 5). Region for seed = Ohio/Columbus bbox.

### 7.2 Scraper interface — `pipeline/scrapers/base.py`
```python
class NormalizedRecord(TypedDict):
    source_ref: str; name: str; org_type: str; org_name: str | None
    lat: float | None; lon: float | None
    address_line, city, state, postal_code: str | None
    hours: dict | None; hours_raw: str | None
    accepted_items: list[str] | None; phone: str | None; website: str | None

class BaseScraper(ABC):
    code: str                       # sources.code
    def fetch(self, region) -> Iterable[NormalizedRecord]: ...
```
A shared `loader.load(scraper, region)` drives every scraper identically: open `scrape_log` → `fetch()` → for each record, **honor `sources.storage_policy`**:
- `ingest` → dedup-match → upsert `locations` + `location_sources` (+ triggers recompute confidence).
- `enrich_only` → dedup-match for reporting only; **persist nothing**; tally `enrich_matches` into `scrape_log.detail`. (D1)

### 7.3 Concrete scrapers (directive minimum = Goodwill + Salvation Army)
- **`salvation_army.py`** (`code='salvation_army'`, **ingest**): ZIP-centroid sweep of `GET satruck.org/apiservices/pickup/donategoods/locations?Type=3&ZipCode=NNNNN&otid=0`; dedupe on `LocationGUID`; parse free-text Hours; map `TypeName` → org_type. Seed region: Ohio ZIPs.
- **`goodwill.py`** (`code='goodwill'`, **enrich_only**): harvest nonce from `/locator/`, geo-tile `GET goodwill.org/wp-admin/admin-ajax.php?action=gwlf_get_locations&security=<nonce>&lat=&lng=&radius=&cats=1`; filter `ci_servD` donation sites. Runs the full fetch→normalize→dedup path but **writes no canonical rows** — proves the pattern + enforces D1.
- *Planet Aid / USAgain / Wearable Collections follow the same `BaseScraper` interface (ingest) — included as ready extension points; not required for the Ohio seed since USAgain has no OH coverage and Planet Aid's OH footprint is sparse.*

### 7.4 Dedup — `pipeline/dedup.py` (validated predicate, FINDINGS Finding 4)
- Runs post-ingest. Candidate generation via PostGIS `ST_DWithin(geom::geography, …, 600)` (only nearby pairs), then apply:
  ```
  match := brand_equal AND ( (dist ≤ 300 AND name_sim ≥ 0.4)
                              OR (dist ≤ 600 AND street_number_equal) )
  ```
- `name_sim = max(SequenceMatcher ratio, token-set Jaccard)` on `normalize_name`, after **brand canonicalization** (Goodwill/Salvation Army/VoA/Habitat/… → single token) — the load-bearing step from Finding 4.
- **Merge:** choose canonical = highest `Σ authority_weight` then oldest; repoint loser's `location_sources` to canonical; set loser `status='merged'`, `merged_into_id`; recompute canonical confidence. Idempotent + re-runnable.

### 7.5 Confidence
Single source of truth = SQL `recompute_confidence()` (DATA_MODEL §7), fired by triggers on vote/source writes. Pipeline never computes confidence in Python — it just writes sources/votes and lets the DB recompute. Guarantees the directive's "recalculation on every vote write."

---

## 8. Deployment

**Target:** single VPS. **Recommended: Hetzner CX22** (2 vCPU, 4 GB RAM, 40 GB SSD, ~€4–5/mo) — cheapest credible tier for civic infra; **alt: DigitalOcean 2 GB/2 vCPU droplet** (~$18/mo) if Hetzner unavailable. PostGIS + FastAPI + nginx fit comfortably; the public API serves from PostGIS so load is light.

**`docker-compose.yml`** (3 services + seed):
```
db    : postgis/postgis:17-3.5     # pinned; bump to 17-3.6 when published (FINDINGS D3)
        volume pgdata; healthcheck pg_isready; runs migrations on first boot via /docker-entrypoint-initdb.d
api   : build ./backend            # uvicorn app.main:app; depends_on db healthy; reads .env
web   : nginx:alpine               # serves frontend/ static; reverse-proxies /api -> api:8000
        sets X-Forwarded-For for ip_hash
```
`scripts/seed.sh` runs the Ohio OSM ingest + Salvation Army scrape + dedup via `docker compose run --rm api python -m pipeline.seed` so the map has real data on first boot (directive Phase 3 end condition).

**Migrations:** plain numbered SQL in `migrations/` applied by the Postgres init mount on first boot and by an idempotent `scripts/migrate.sh` for existing DBs (boring, no migration framework).

**`.env.example` (all required vars documented):**
```
DATABASE_URL=postgresql://opendrop:opendrop@db:5432/opendrop
POSTGRES_USER=opendrop  POSTGRES_PASSWORD=opendrop  POSTGRES_DB=opendrop
API_PORT=8000
CORS_ORIGINS=http://localhost:8080
IP_HASH_SALT=change-me-in-prod
TURNSTILE_SECRET=1x0000000000000000000000000000000AA   # CF always-passes test secret (dev mock)
TURNSTILE_SITEKEY=1x00000000000000000000AA              # CF test sitekey
OVERPASS_URL=https://overpass-api.de/api/interpreter
NOMINATIM_URL=https://nominatim.openstreetmap.org/search
SEED_REGION_BBOX=39.80,-83.25,40.18,-82.75              # Columbus metro (s,w,n,e)
SUBMIT_PER_IP_PER_DAY=10
POINT_CAP=2000  CLUSTER_CAP=400
```

---

## 9. Cross-cutting decisions (carry-through from Phase 1)

| FINDINGS item | Architectural resolution |
|---|---|
| Overpass batch-only (⚠) | API never calls Overpass; only `pipeline/osm_ingest.py` does, offline. |
| Goodwill enrich-only (D1) | `sources.storage_policy='enrich_only'`; loader persists nothing; `v_public_locations` + `is_redistributable` guard exports. |
| clothedonations not stored (D2) | Not a source; used only as an out-of-band coverage QA reference (not in the system). |
| PG16→17 (D3) | `postgis/postgis:17-3.5` image, schema targets PG17. |
| OSM hours/collection_times absent | Confidence formula excludes hours; relies on source authority + votes + staleness. |
| No bulk org endpoints | Shared ZIP/geo-sweep + dedupe loader, not bespoke loaders. |
| Coordinates missing (Wearable Coll.) | Geocode via Nominatim; never store Google-derived geometry. |
