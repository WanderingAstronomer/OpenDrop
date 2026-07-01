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
                                      │ (batch only)    │ planetaid, usagain,
                                      │                 │ wearable_collections, goodwill*)
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
| **PostGIS pairs cleanly** | `psycopg` (v3) gives first-class async + raw SQL; we lean on PostGIS functions (`ST_DWithin` for dedup candidates, `ST_SnapToGrid` for map clustering) rather than an ORM's geo abstractions. |
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
      images.py      # GET/POST /locations/{id}/images, POST /images/{id}/vote
      meta.py        # GET meta/health/stats, GET /geosearch
  tests/
  pyproject.toml
  Dockerfile
```

---

## 3. Database

PostgreSQL 17 + PostGIS 3.6 (decision D3). Full schema in [DATA_MODEL.md](DATA_MODEL.md): tables `sources`, `locations`, `location_sources`, `votes`, `pending_locations`, `scrape_log`, plus the community-photo tables `location_images` and `image_votes` (added by migration 0004); `recompute_confidence()` + vote/source triggers, plus `recompute_image()` + the `trg_after_image_vote` trigger that auto-applies a pin correction once a correction photo reaches score ≥ 3; `v_public_locations` redistributable view. The schema is an **ordered migration chain** applied in order against a `schema_migrations` ledger (not just `0001_init.sql`): `0001_init.sql` (base PostGIS schema), `0002_fix_source_component.sql` (confidence component `LEAST(85,NULL)` → `COALESCE(SUM(...),0)` fix), `0003_add_consignment.sql` (adds the `consignment` org_type after `thrift_store`), `0004_images.sql` (`location_images` + `image_votes` + the `image_status` enum + `recompute_image()` + `trg_after_image_vote`), `0005_image_vote_turnstile.sql` (adds `image_votes.turnstile_hash`). Distance math uses `geography` casts so all thresholds are in meters.

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
      "properties": {"id":123,"name":"Salvation Army Family Store - Dublin","org_type":"charity_store",
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
{ "id":123, "name":"Salvation Army Family Store - Dublin", "org_type":"charity_store", "org_name":"The Salvation Army",
  "lat":39.96, "lon":-82.99,
  "address":{"line":"6625 Dublin Center Dr","city":"Dublin","state":"OH","postal_code":"43017"},
  "hours":{"mon":[["09:00","21:00"]], "always":false}, "hours_raw":"Mon-Sat 9-9",
  "accepted_items":["clothing","shoes","household"], "phone":null, "website":null,
  "confidence":62.0, "bucket":"medium", "status":"active",
  "upvotes":4, "denies":1, "last_verified_at":"2026-06-20T12:00:00Z",
  "sources":[{"code":"osm","display_name":"OpenStreetMap","attribution":"© OpenStreetMap contributors (ODbL)"},
             {"code":"salvation_army","display_name":"The Salvation Army","attribution":"Data: The Salvation Army (satruck.org)"}] }
```
`sources[].attribution` is always present (the column is `NOT NULL`), so `models.py` types it required. The detail endpoint serves rows of **any** status except `merged` — including `pending`/hidden ones by direct id, which is the recovery path that lets a denied location be re-confirmed via `POST …/vote` even though it is absent from the default map. Errors: `404 not_found`. A `merged` row returns `404` with the uniform envelope `{ "error": { "code":"merged", "message":"…", "details": { "canonical_id": <id> } } }`; the v1 client treats `merged` as not-found (no auto-redirect).

### 4.3 `POST /api/locations/{id}/vote` — crowd vote

Request: `{ "vote":"confirm"|"deny", "turnstile_token":"<token>" }`
Pipeline (single transaction): verify Turnstile → derive `ip_hash` from the **trusted** client IP (§6) → **`pg_advisory_xact_lock(hashtext(location_id::text || ip_hash))`** (serializes concurrent votes for the same voter+location so the check-then-insert can't race under READ COMMITTED) → **cooldown check** (any vote this `(location_id, ip_hash)` in 24h?) → insert `votes` row (trigger recomputes confidence + status) → return:
```json
{ "id":123, "confidence":67.0, "bucket":"medium", "status":"active", "upvotes":5, "denies":1 }
```
Errors: `400 bad_request` (missing/invalid vote), `403 turnstile_failed`, `404 not_found`, `429 cooldown_active` (includes `retry_after` seconds).

### 4.4 `POST /api/locations` — submit new location

Request: `{ "name":"...", "org_type":"drop_bin", "address":{"line":"...","city":"...","state":"OH","postal_code":"..."}, "turnstile_token":"<token>" }`
Pipeline: verify Turnstile → derive `ip_hash` → **geocode** address (Nominatim) → **dedup-check** against `locations` (same predicate as the batch dedup) → insert `pending_locations`:
- geocoded + no dupe → `status=awaiting` → **auto-promoted** immediately (§7.6): a `locations` row is created (`crowd` source, `status='pending'`, confidence ≈20, hidden until a confirm lifts it ≥25). Response carries the new `location_id`.
- dupe found → `status=duplicate`, `dupe_candidate_id` set; response returns `duplicate_of: <canonical id>` (the toast links to it by id).
- geocode failed → `status=awaiting`, `geom=NULL` (left for manual geocode/review; **not** auto-promoted).
```json
{ "pending_id":55, "status":"promoted", "geocoded":true,
  "location_id":920, "duplicate_of": null }
```
Errors: `400 bad_request`, `403 turnstile_failed`, `422 geocode_failed` (still records the submission as `awaiting`), `429 submit_cooldown` (per-IP submit throttle, see §6).

### 4.5 `GET /api/meta` — attribution + stats (powers the map's attribution control)

`sources` lists **only sources that actually contribute displayed data** — `storage_policy='ingest'` AND referenced by ≥1 `active` location's `location_sources`. This deliberately excludes `enrich_only` (Goodwill) so the map never displays a "Goodwill" attribution for data it does not store or show, and omits sources with zero live rows.

```json
{ "counts":{"active":1234,"pending":56,"by_type":{"charity_store":...}},
  "sources":[{"code":"osm","attribution":"© OpenStreetMap contributors (ODbL)","license":"ODbL-1.0"}, ...],
  "turnstile_sitekey":"1x00000000000000000000AA",
  "confidence_buckets":{"high":70,"medium":40,"low":25} }
```

### 4.6 `GET /api/export` — redistributable open-data dump

Reads `v_public_locations` only (active + redistributable). Streams GeoJSON. **ODbL requires attribution to travel with the data**, so attribution + license are embedded as top-level foreign members of the FeatureCollection (not only an HTTP header, which is lost the moment the file is saved):
```json
{ "type":"FeatureCollection",
  "license":"ODbL-1.0 (OSM) + per-source attribution",
  "attribution":["© OpenStreetMap contributors (ODbL)","Data: The Salvation Army (satruck.org)", "..."],
  "generated_at":"2026-06-27T19:00:00Z",
  "features":[ ... ] }
```
The `attribution` array is built from the `sources` of the exported rows. An `X-Data-Attribution` header is also set for convenience. Guarantees no `enrich_only` data leaks (D1/D2 — enforced by `v_public_locations` + the §7.2 loader branch + the field-provenance invariant). Optional `?state=OH`.

### 4.7 `GET /api/health` — liveness (DB ping). `200 {"status":"ok","db":true}`.

### 4.8 `GET /api/locations/{id}/images` — community photo gallery

Returns the `visible` community photos for a location (path, mime, score, upvotes/downvotes, suggested pin coords for corrections, applied flag). `?include_low=true` additionally surfaces `pending`/`hidden` images (moderation/debug view). Errors: `404 not_found`.

### 4.9 `POST /api/locations/{id}/images` — upload photo (+ optional pin correction)

Multipart upload of a photo with an optional `suggested_lat`/`suggested_lon` pin correction and `turnstile_token`. Pipeline: verify Turnstile → derive `ip_hash` → enforce the per-IP daily cap (`IMAGE_UPLOADS_PER_IP_PER_DAY`) and size limit (`IMAGE_MAX_BYTES`) → strip EXIF and persist via the imageproc helper into `MEDIA_DIR` → insert a `location_images` row (`status='pending'`). Errors: `400 bad_request`, `403 turnstile_failed`, `404 not_found`, `413`/`422` (too large / bad image), `429` (per-IP daily cap).

### 4.10 `POST /api/images/{id}/vote` — helpful/unhelpful on a photo

Request: `{ "helpful": true|false, "turnstile_token":"<token>" }`. **Turnstile-gated** (mirrors the location vote). Pipeline (single transaction): verify Turnstile → derive `ip_hash` → `pg_advisory_xact_lock` on `(image_id, ip_hash)` → upsert an `image_votes` row (`UNIQUE(image_id, ip_hash)`) → `recompute_image()` recomputes the image score; when a **pin-correction** photo reaches **score ≥ 3** the `trg_after_image_vote` trigger auto-applies the correction, moving the canonical location pin to the suggested coords and marking the image `applied`. Errors: `400 bad_request`, `403 turnstile_failed`, `404 not_found`.

### 4.11 `GET /api/geosearch?q=` — place search (Nominatim proxy + cache)

Proxies a forward-geocoding query to Nominatim (cached) to power the map's search box. Returns ranked place matches (display name + lat/lon). Errors: `400 bad_request`.

---

## 5. Frontend

Single full-viewport page. **Zero UI framework** beyond Leaflet + Turnstile (directive). Vanilla ES modules, served static (no build step required; an optional esbuild bundle is allowed but not needed).

**Dependencies (vendored/CDN, exact-pinned):** Leaflet **1.9.4**, Leaflet.markercluster **1.5.3** (exact, not a range), Cloudflare Turnstile script (`challenges.cloudflare.com/turnstile/v0/api.js`). All three live-verified current in Phase 2 review.

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
| `list.js` | keyboard/screen-reader-friendly list view of in-view locations + category (org_type) filter |
| `photos.js` | community-photo gallery: render/upload/vote on photos + click-map pin-correction flow (`/locations/{id}/images`, `/images/{id}/vote`) |
| `search.js` | place-search box backed by `GET /api/geosearch` |
| `confidence.js` | confidence → bucket → color/label helper (high≥70 green / medium≥40 amber / low≥25 red) |
| `toast.js` | minimal notifications |

**Clustering strategy:** below the point cap the server returns raw points and the client uses Leaflet.markercluster (smooth spiderfy at street level — "individual pins at street level"). When the server returns `mode:'clusters'` (zoomed out / dense), the client draws lightweight count bubbles ("cluster markers at low zoom"). The boundary is the server's `POINT_CAP`, so the client never has to hold the whole US in memory.

**Turnstile placement:** rendered **inline in the popover before vote submission** and inline in the submit panel (directive). The widget yields a single-use token passed to the API; the API re-verifies server-side (never trust the client).

**Map base tiles:** OpenStreetMap standard tiles with the required `© OpenStreetMap contributors` attribution; the attribution control is augmented from `/api/meta` sources (ODbL + each org). Note polite tile-usage; a self-hosted/tiles-provider swap is a documented later step, not a v1 blocker.

---

## 6. Crowd validation subsystem

- **Vote write** (§4.3): Turnstile verify → ip_hash → advisory xact lock → 24h cooldown → append `votes` → trigger `recompute_confidence` (DATA_MODEL §7). Confidence/status update is atomic with the vote.
- **Trusted client IP (must not trust client-supplied XFF):** `ip_hash = sha256(IP_HASH_SALT || client_ip)`, where `client_ip` is the **trusted proxy-set** value, **not** the left-most `X-Forwarded-For` (which the browser can forge to rotate `ip_hash` per request and bypass the cooldown). In our single-proxy topology, nginx sets `proxy_set_header X-Real-IP $remote_addr` (the real peer, which nginx **overwrites** so a client header can't spoof it); the API reads `X-Real-IP`, falling back to the socket peer. Any inbound `X-Forwarded-For` is ignored for trust. Document the single-proxy assumption.
- **IP cooldown:** 24h rolling window per `(location_id, ip_hash)`, made race-safe by `pg_advisory_xact_lock` (§4.3). Submit endpoint has its own coarser throttle: max `SUBMIT_PER_IP_PER_DAY=10`.
- **Turnstile integration points:** (1) vote popover, (2) submission panel, (3) community-photo upload, (4) photo (image) vote. Server verifies via `POST https://challenges.cloudflare.com/turnstile/v0/siteverify` with `secret`, `response`, `remoteip`. **In production**, Cloudflare enforces token single-use + 300 s TTL (FINDINGS Finding 5). **Dev mock mode:** when `TURNSTILE_SECRET` is Cloudflare's test secret `1x0000000000000000000000000000000AA` (always-passes), the server still **requires a non-empty token** and rejects empty/missing ones — satisfying Phase 4 step 3 ("blocks submission without a valid token in dev mock mode"). Note: the test secret does **not** enforce single-use (any string passes, replayable), so in dev the only replay defense is the IP cooldown — acceptable for v1; the trusted-IP + advisory-lock cooldown is the real gate. Test sitekey `1x00000000000000000000AA` renders a passing widget locally.
- **Abuse resistance (good-enough, per directive):** Turnstile blocks scripted floods; trusted-IP cooldown blocks repeat voting (no per-request hash rotation); the deny-dominance override (DATA_MODEL §7) lets the crowd retire a dead location while requiring ≥5 distinct IP-hashes, so a single casual actor can't nuke it. Not adversarially hardened (no Sybil-proofing, no per-token single-use in dev) — explicitly out of scope for v1.

---

## 7. Data pipeline

All jobs are Python modules run via `python -m pipeline.<job>` (one-shot or cron), writing to PostGIS. Each run records a `scrape_log` row.

### 7.0 Regions — `pipeline/regions.py`
Every pipeline job runs against a **region** that defines its bbox, search center, and (where applicable) the ZIP sweep list. A `Region` dataclass carries `name`, `bbox`, `center`, `zips`, `radius_mi`. `REGIONS` ships three **curated** regions:
- **`columbus`** — the **default**; the Columbus metro bbox. Its bbox (and only its bbox) is overridable via the `SEED_REGION_BBOX` env var.
- **`ohio`** — statewide Ohio.
- **`greater_ohio`** — multi-state: Ohio plus the bordering states (MI/IN/KY/WV/PA). `bbox=(36.50,-88.20,44.00,-74.70)` (south,west,north,east), `center=(40.20,-81.50)`, `radius_mi=300`, with a cross-state ZIP sweep list (`GREATER_OHIO_ZIPS`).

**National regions (data-driven).** On top of the curated set, the module derives a region for **every state + DC** and a `usa` union **from the vendored ZIP table** `pipeline/data/us_zips.csv` (not hand-maintained). `state_regions()` groups ZIPs by state, computes each state's bbox from its own ZIP centroids (+ a small pad), uses the centroid of those ZIPs as the search center, and carries that state's full ZIP list for the sweep scrapers; `usa` is the union bbox over all states with the whole ~42k-ZIP list. Resolution order in `get_region()`: curated name → state code (`ca`) / `usa` → friendly name (`california`, and `ohio_full` for the per-state Ohio so it never shadows the curated `ohio`) → fallback to `columbus` on an unknown name. The active region is chosen by the `REGION` env var.

### 7.1 OSM ingest — `pipeline/osm_ingest.py`
- Query Overpass (`OVERPASS_URL`, default overpass-api.de) for a region bbox: `shop=charity`, `shop=second_hand`, `amenity=recycling` + `recycling:clothes`/`recycling:shoes`. `[out:json][timeout:90]; ... out center tags;` (proven in Phase 1). Descriptive User-Agent.
- Normalize each element → canonical record (map tags → org_type, name, address, hours via `opening_hours`/`collection_times` when present, brand→org_name). `out center` gives a point for ways.
- **Upsert** keyed on `location_sources(source_code='osm', source_ref='<type>/<id>')`. New ref → run dedup-match against existing canonical; attach to match or create a new `locations` row + `location_sources` row. Existing ref → update `last_seen_at`/payload.
- Batch-only against the public endpoint (Finding 5). Region for seed = Ohio/Columbus bbox.
- **Tiling for large regions.** A state or national bbox is far too large for one Overpass query, so `fetch` splits any region into ≤ `OSM_TILE_DEGREES` (default 3°) tiles, queries each through the polite client, and merges — deduping elements that fall in two adjacent tiles by `(type, id)`. The committed Columbus fixture fallback (used when Overpass is unreachable) now applies **only to regions whose bbox actually covers Columbus** (`_covers_fixture`), so a national/other-state run can never substitute Columbus bins for a region it failed to fetch.

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

**Closure detection (reconciliation) — `_reconcile()`.** After a clean ingest run the loader retires `location_sources` links that the source no longer reports (a closed/removed location). This is guarded by a **circuit breaker** so a partial or degraded fetch can't mass-retire live data: reconciliation is skipped entirely if the run recorded **any** per-record errors, refuses to retire links when the run saw fewer than `RECONCILE_MIN_SEEN` records (default **5**), and refuses to retire more than `RECONCILE_MAX_FRACTION` (default **0.40**) of a source's *current* in-region links (counted before any deletion, in the same transaction). It is **region-scoped** to the run's bbox. Both thresholds are env-overridable (`RECONCILE_MIN_SEEN`, `RECONCILE_MAX_FRACTION`, read via `os.environ` in `base.py`).

### 7.3 Concrete scrapers (directive minimum = Goodwill + Salvation Army)
- **`salvation_army.py`** (`code='salvation_army'`, **ingest**): ZIP-centroid sweep of `GET satruck.org/apiservices/pickup/donategoods/locations?Type=3&ZipCode=NNNNN&otid=0`; dedupe on `LocationGUID`; parse free-text Hours; map `TypeName` → org_type. Seed region: Ohio ZIPs.
- **`goodwill.py`** (`code='goodwill'`, **enrich_only**): harvest nonce from `/locator/`, geo-tile `GET goodwill.org/wp-admin/admin-ajax.php?action=gwlf_get_locations&security=<nonce>&lat=&lng=&radius=&cats=1`; filter `ci_servD` donation sites. **Scope for v1: a scraper-interface pattern demo only** — it runs the full fetch → normalize → dedup-match path and writes **only** a `scrape_log` row (`records_upserted=0`, `enrich_matches` count). It persists **nothing** to `locations`/`location_sources` and surfaces nothing to users. This honors D1's "never store" rule and the directive's requirement of a second real scraper. *(Live query-time enrichment — merging Goodwill fields into a `/api/locations/{id}` response without persisting — is a documented **future extension**, explicitly out of scope for v1; if built, the field-provenance invariant forbids those values from ever being written to canonical columns or `/api/export`.)*
- **`planet_aid.py`**, **`usagain.py`**, **`wearable_collections.py`** (all `ingest`): implemented concrete scrapers following the same `BaseScraper` interface, **wired into `pipeline/seed.py`** alongside OSM and Salvation Army. Coverage caveats: USAgain currently returns **no Ohio** records, and Wearable Collections is **NYC-only** — so neither contributes to the Columbus/Ohio seed today, but both run as part of the pipeline (and light up automatically for the `greater_ohio` region's bordering-state sweep / out-of-region runs).

### 7.4 Dedup — `pipeline/dedup.py` (validated predicate, FINDINGS Finding 4)

**Brand canonicalization (load-bearing).** During normalization every record gets a `brand_key`: lowercase `org_name`/OSM `brand`/`operator` mapped through a canonicalization table (Goodwill / Salvation Army / Volunteers of America / Habitat for Humanity / St. Vincent de Paul / …) to one token; unrecognized/empty → `brand_key = NULL` (**unbranded**, e.g. most drop bins). `brand_equal(a,b) := a.brand_key IS NOT NULL AND a.brand_key = b.brand_key` — two NULL brands are **never** `brand_equal` (so unbranded bins don't collapse via the empty-string-name `name_sim=1.0` trap the Phase-2 review flagged).

**Candidate generation:** `ST_DWithin(a.geom::geography, b.geom::geography, 600)` over `status NOT IN ('merged','hidden')` rows only (uses `locations_active_geom_gix`/skips tombstones).

**Match predicate:**
```
match(a,b) :=
   ( brand_equal(a,b)
     AND ( (dist ≤ 300 AND name_sim ≥ 0.4)                              -- primary (validated 0 FP)
           OR (dist ≤ 600 AND name_sim ≥ 0.4 AND house_number_equal) )) -- tier-2 (recovers the 1 FN)
   OR ( a.brand_key IS NULL AND b.brand_key IS NULL                     -- unbranded co-located bins
        AND a.org_type = b.org_type AND a.org_type IN ('drop_bin','donation_center')
        AND dist ≤ 25 )                                                 -- very tight; bins on the same spot
```
- `name_sim = max(SequenceMatcher ratio, token-set Jaccard)` on `normalize_name` (computed in Python over the `ST_DWithin` candidate set; the pg_trgm index is **not** used for the batch sim — it serves the API submit-time pre-filter in §4.4).
- `house_number_equal`: compare `normalize_house_number(address_line)` (leading integer token) on both sides; **NULL on either side ⇒ not equal** (OSM's ~36% address coverage means tier-2 often can't fire — accepted, it only *recovers* a FN, never the sole gate). Tier-2 retains the `name_sim ≥ 0.4` gate to match the validated Phase-1 report exactly.

**Merge (idempotent + re-runnable):** choose canonical = highest `Σ authority_weight` then oldest `id`. Then: (1) **field-provenance** — recompute canonical display columns from its ingest sources by authority (DATA_MODEL invariant); (2) repoint loser's `location_sources.location_id` → canonical; (3) **chain-compact** — `UPDATE locations SET merged_into_id = canonical WHERE merged_into_id = loser` (no stale A→B→C chains); (4) set loser `status='merged'`, `merged_into_id=canonical`, and zero its `source_count`/recompute it (the source-repoint trigger only recomputes the canonical, so the loser is reset explicitly); (5) confidence recompute fires on canonical via the source trigger. Re-running is a no-op because tombstoned losers are excluded from candidate generation. Validate against a 2-record fixture for **both** a branded pair and an unbranded-bin pair before Phase 3 closes.

### 7.5 Confidence
Single source of truth = SQL `recompute_confidence()` (DATA_MODEL §7), fired by triggers on vote/source writes. Pipeline never computes confidence in Python — it just writes sources/votes and lets the DB recompute. Guarantees the directive's "recalculation on every vote write."

### 7.6 Crowd-submission promotion — `pipeline/promote.py` (+ called inline by `POST /api/locations`)
Moves `pending_locations(awaiting)` into the canonical store. Criteria + writes:
- **Auto-promote on submit** when the submission is geocoded (`geom NOT NULL`) AND dedup-check finds no duplicate: create a `locations` row (`org_type`, `name`, `brand_key` from canonicalization, `geom`, address); insert a `crowd` `location_sources` row (authority 20); the source trigger recomputes confidence (≈20 → `status='pending'`, hidden until a confirm vote lifts it ≥25). Set `pending_locations.status='promoted'`, `promoted_location_id`.
- **Duplicate** → `status='duplicate'`, `dupe_candidate_id` set; no canonical row created.
- **Geocode failed** (`geom NULL`) → stays `awaiting`; a batch `promote.py` run re-attempts geocode/manual review later. The same module is runnable standalone (`python -m pipeline.promote`) to drain the `awaiting` backlog.
This is the path that makes the `crowd` source, `pending_status` transitions, and `promoted_location_id` live (closes the Phase-2 review blocker).

### 7.7 National seed — `pipeline/seed_national.py` (gentle, resumable)
`scripts/seed.sh` runs every scraper once over a single `REGION`. Seeding all 50 states + DC is instead a long-running, restartable job (`bash scripts/seed_national.sh` → `python -m pipeline.seed_national`):
- **Per-state iteration.** Walks `state_regions()` and runs the full ingest scraper set (OSM + Salvation Army + Planet Aid + USAgain + Wearable Collections; Goodwill excluded — enrich-only) against one state at a time. Per-state granularity bounds each request burst, scopes closure-detection to one state, and gives the checkpoint its unit.
- **Checkpointing.** Progress is recorded in `seed_progress` (**migration 0008**): a state is marked `running` before its sweep and `done` only after the whole set completes. On resume, `done` states are skipped, a state left `running` (interrupted) is re-run, and a synthetic `__finalize__` row guards the once-at-the-end global `dedup.run` + `promote.run`. `SEED_FORCE=1` ignores checkpoints and re-sweeps everything. `Ctrl-C` (uncaught `KeyboardInterrupt`) deliberately leaves the in-flight state `running` so the next run repeats it.
- **Politeness.** Every upstream call goes through `scrapers/http.py`'s `PoliteClient` (inter-request pacing, exponential backoff + jitter, `Retry-After`/429/5xx handling), keeping a multi-hour overnight run within source ToS (incl. Nominatim 1 req/s). *Build-only by design — the capability + seeder ship; no live national seed is run as part of the build.*

---

## 8. Deployment

**Target:** single VPS. **Recommended: Hetzner CX22** (2 vCPU, 4 GB RAM, 40 GB SSD, ~€4–5/mo) — cheapest credible tier for civic infra; **alt: DigitalOcean 2 GB/2 vCPU droplet** (~$18/mo) if Hetzner unavailable. PostGIS + FastAPI + nginx fit comfortably; the public API serves from PostGIS so load is light.

**`docker-compose.yml`** (3 services + seed):
```
db    : postgis/postgis:17-3.5     # verified to exist on Docker Hub; PostGIS 3.5 (schema uses no 3.6-only
                                   # feature). Non-alpine 17-3.6 not yet published; 17-3.6-alpine exists if
                                   # 3.6 is ever needed. Do NOT chase a non-existent 17-3.6 tag.
        volume pgdata; initdb mount applies migrations/0001_init.sql on FIRST boot only.
        healthcheck: schema-aware -> `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB && psql -tAc 'select 1 from sources limit 1'`
        (plain pg_isready can report healthy during the initdb temp-server window, before 0001 finishes).
api   : build ./backend            # uvicorn app.main:app; depends_on db (condition: service_healthy);
        ALSO retries the DB connection with backoff on startup (belt-and-suspenders vs the initdb race).
        ports ["${API_PORT:-8000}:8000"] (optional; web proxies it anyway).
web   : nginx:alpine               # serves frontend/ static; reverse-proxies /api -> api:8000.
        ports ["${WEB_PORT:-8080}:80"]  <-- the map opens at http://localhost:8080
        sets `proxy_set_header X-Real-IP $remote_addr;` (trusted client IP for ip_hash; see §6) and DROPS
        inbound X-Forwarded-For from trust. db publishes NO host port.
```
`scripts/seed.sh` runs the full pipeline via `docker compose run --rm api python -m pipeline.seed` so the map has real data on first boot (directive Phase 3 end condition): OSM ingest + Salvation Army + Planet Aid + USAgain + Wearable Collections + Goodwill(enrich) + dedup + promote. CORS is effectively a no-op in this same-origin nginx topology (browser hits `web` for both static and `/api`); `CORS_ORIGINS` exists only for split-origin dev.

**Migrations:** plain numbered SQL in `migrations/` — currently the ordered chain `0001_init.sql` → `0005_image_vote_turnstile.sql` (see §3). A `schema_migrations(version, applied_at)` ledger (DATA_MODEL "Migration mechanism") makes `scripts/migrate.sh` apply files in order and skip already-applied ones — so re-running is safe **without** self-idempotent DDL (`CREATE TYPE … AS ENUM` has no `IF NOT EXISTS`). First container boot applies `0001_init.sql` via the initdb mount; `migrate.sh` (ledger-guarded) brings existing DBs up through `0002`–`0005` and applies any future `NNNN_*.sql`.

**`.env.example` (all required vars documented):**
```
DATABASE_URL=postgresql://opendrop:opendrop@db:5432/opendrop
POSTGRES_USER=opendrop  POSTGRES_PASSWORD=opendrop  POSTGRES_DB=opendrop
API_PORT=8000
WEB_PORT=8080                                           # host port for the map (http://localhost:8080)
CORS_ORIGINS=http://localhost:8080                      # no-op in same-origin nginx setup; for split-origin dev only
IP_HASH_SALT=change-me-in-prod
TURNSTILE_SECRET=1x0000000000000000000000000000000AA   # CF always-passes test secret (dev mock)
TURNSTILE_SITEKEY=1x00000000000000000000AA              # CF test sitekey
OVERPASS_URL=https://overpass-api.de/api/interpreter
NOMINATIM_URL=https://nominatim.openstreetmap.org/search
REGION=columbus                                         # active region: columbus (default) | ohio | greater_ohio | <state code e.g. ca> | usa
SCRAPER_REQUEST_DELAY_S=0.5                              # polite client: min seconds between upstream requests (§7.1)
OSM_TILE_DEGREES=3.0                                     # max tile size when splitting a large region's Overpass query
SEED_FORCE=                                              # set to 1 to make seed_national re-sweep states already 'done'
SEED_REGION_BBOX=39.80,-83.25,40.18,-82.75              # overrides the COLUMBUS region bbox ONLY (s,w,n,e)
SYNC_INTERVAL_SECONDS=86400                             # cron cadence for the periodic re-sync run
SUBMIT_PER_IP_PER_DAY=10
POINT_CAP=2000  CLUSTER_CAP=400
RECONCILE_MIN_SEEN=5                                    # closure-detection circuit breaker (§7.2)
RECONCILE_MAX_FRACTION=0.40                             # max fraction of a source's in-region links one run may retire
MEDIA_DIR=/app/media                                    # community-photo storage dir
IMAGE_MAX_BYTES=6000000                                 # per-upload size limit
IMAGE_UPLOADS_PER_IP_PER_DAY=8                          # per-IP daily photo-upload cap
APP_ENV=dev                                             # dev | prod
DOMAIN=opendrop.example                                 # prod public hostname (TLS/vhost)
ACME_EMAIL=admin@opendrop.example                       # Let's Encrypt registration email
```

---

## 9. Cross-cutting decisions (carry-through from Phase 1)

| FINDINGS item | Architectural resolution |
|---|---|
| Overpass batch-only (⚠) | API never calls Overpass; only `pipeline/osm_ingest.py` does, offline. |
| Goodwill enrich-only (D1) | **Primary gate** = the §7.2 loader `storage_policy` branch: Goodwill never writes `location_sources`, so no canonical row/field is ever created from it. **Backstops:** the field-provenance invariant (no enrich value into a canonical column) and `is_redistributable`+`v_public_locations` (catches the goodwill-only edge case). |
| clothedonations not stored (D2) | Not a source; used only as an out-of-band coverage QA reference (not in the system). |
| PG16→17 (D3) | PostgreSQL 17; `postgis/postgis:17-3.5` (PostGIS 3.5, verified-existing; schema uses no 3.6-only feature). |
| OSM hours/collection_times absent | Confidence formula excludes hours; relies on source authority + votes + staleness. |
| No bulk org endpoints | Shared ZIP/geo-sweep + dedupe loader, not bespoke loaders. |
| Coordinates missing (Wearable Coll.) | Geocode via Nominatim; never store Google-derived geometry. |
| Crowd submissions must reach the map | §7.6 promotion (auto on submit when geocoded + non-dup) creates the `crowd` location; visible once a confirm vote lifts it ≥25. |
