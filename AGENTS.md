# OpenDrop — Master Agent Directive

## Authorization

You are operating under full agentic authorization with bypass permissions enabled in Claude Code. You are expected to run autonomously across multiple turns and phases without waiting for human confirmation between steps unless a hard blocker is explicitly flagged (see Escalation Policy below). Long runtimes are acceptable. Do the work.

---

## Mission

OpenDrop is a community-owned, open-data map of every clothing donation location in the United States — drop bins, thrift stores, mutual aid closets, church drives, seasonal collection points — regardless of org size or national profile. The core thesis is that no authoritative aggregator exists, the data is scattered across siloed org locators and OpenStreetMap, and the only scalable solution to data freshness is crowd-sourced validation backed by a confidence scoring system. The project is civic infrastructure, not a product. Every architectural decision should reflect that.

---

## Constraints and Principles

- Maximize autonomous output. Do not stub, scaffold, or defer implementation unless a genuine external dependency (live credentials, DNS, third-party API key) blocks completion.
- Prefer boring, proven technology. This is not a showcase project.
- All data must be redistributable. OSM data is ODbL-licensed — attribute it. Google Places and Foursquare data is not redistributable raw — treat those as enrichment at query time only, never store verbatim.
- The crowd validation system must be resistant to casual abuse (brigading, duplicate voting) but does not need to be adversarially hardened. Good enough is the correct standard.
- No auth system for v1. IP-based rate limiting plus Cloudflare Turnstile CAPTCHA is sufficient gating.
- Every phase must end with a written validation summary before the next phase begins.

---

## Escalation Policy

Stop and surface a human decision only when:
- A required API key or credential is absent and cannot be mocked for local dev
- Two viable architectural paths exist with non-trivial long-term cost difference
- A data source produces results that invalidate a prior planning assumption materially

Everything else: make the call, document the decision inline, and continue.

---

## Phase 1 — Research and Verification

**Objective:** Ground every subsequent decision in verified, current data. No assumptions from training data pass through to Phase 2 without confirmation.

Complete the following in sequence. Write findings to `research/FINDINGS.md` as you go.

1. **OSM Data Audit.** Query the Overpass API for `shop=charity`, `amenity=recycling` with `recycling:clothes=yes`, and `collection_times` tags across a representative US bounding box (use Ohio as the test region — Columbus metro). Count results, assess completeness, identify field consistency, and document schema gaps.

2. **Org API Survey.** For each of the following, identify whether a public-facing locator endpoint exists, whether it is scrapeable without authentication, and what the data fields are: Goodwill, Salvation Army, Planet Aid, DAV, USAgain, GreenDrop, Wearable Collections. Document ToS flags where present.

3. **Existing Tool Audit.** Evaluate clothedonations.com, donationtown.org, and earth911.com for data source methodology, staleness signals, and gap coverage. Determine if any of them expose an API or feed worth ingesting.

4. **Deduplication Problem Scope.** Using the Ohio OSM results plus one scraped org feed, run a sample deduplication pass (coordinate proximity threshold + name fuzzy match). Document false positive/negative rates and what tuning is needed.

5. **Stack Validation.** Confirm the following are current, actively maintained, and fit-for-purpose: PostGIS 3.x on PostgreSQL 16, Leaflet.js latest, Cloudflare Turnstile (free tier limits), Overpass API public endpoint rate limits. Flag any that have materially changed or been superseded.

**Phase 1 ends when** `research/FINDINGS.md` is complete and a one-paragraph go/no-go statement is written at the top of the file.

---

## Phase 2 — Architecture and Planning

**Objective:** Produce a complete, unambiguous build specification that Phase 3 can execute without further planning. Validate the spec against Phase 1 findings before closing the phase.

Produce the following documents in `planning/`:

### `ARCHITECTURE.md`

- System diagram described in text (Mermaid if helpful)
- Backend: language choice with justification (Node/Express or Python/FastAPI — pick one and defend it)
- Database: PostGIS schema, all tables, indexes, and constraints defined in full
- API: every endpoint, method, expected payload, response shape, and error behavior
- Frontend: component map, map library choice, clustering strategy
- Data pipeline: OSM ingest job, per-org scraper job architecture, deduplication algorithm spec, confidence score formula
- Crowd validation: vote schema, score update logic, IP cooldown mechanism, Turnstile integration points
- Deployment target: single VPS (specify provider and tier), Docker Compose layout

### `DATA_MODEL.md`

Full PostGIS schema in SQL DDL. Minimum tables:

- `locations` — canonical location record with geometry point, name, org type, confidence score, source flags, timestamps
- `location_sources` — one row per source contributing to a canonical location (OSM node ID, org feed ID, etc.)
- `votes` — upvote/deny per location per IP hash, with timestamp and Turnstile token hash
- `pending_locations` — crowd-submitted new locations awaiting confidence threshold
- `scrape_log` — per-org scrape run history for freshness tracking

### `BUILD_SEQUENCE.md`

Ordered list of every discrete build task in Phase 3, with explicit dependencies noted. This is the execution queue Phase 3 works from. No task should be ambiguous enough to require re-planning during execution.

**Phase 2 validation pass:** Before closing, re-read every Phase 1 finding and confirm each is addressed in the architecture. Write a `VALIDATION.md` in `planning/` that maps each finding to its architectural resolution. Any unresolved finding is a blocker.

**Phase 2 ends when** all three documents are complete and `VALIDATION.md` contains no open items.

---

## Phase 3 — Construction

**Objective:** Build everything specified in Phase 2. Work from `BUILD_SEQUENCE.md` in order. Mark each task complete inline as you finish it.

The build target is a fully functional local development instance. Deployment config (Docker Compose, env template) must be production-ready but does not need to be deployed to live infrastructure — that requires human credentials.

### Expected Deliverables

**Backend**
- REST API server with all endpoints from the architecture spec implemented and tested
- PostGIS schema applied via migration file
- Cloudflare Turnstile server-side verification middleware
- IP rate limiting middleware (24hr cooldown per location per IP)
- Confidence score recalculation triggered on every vote write

**Data Pipeline**
- OSM Overpass ingest script: fetches, normalizes, and loads into `locations` and `location_sources`
- Deduplication script: runs post-ingest, merges duplicates, updates confidence scores
- At minimum two org scraper modules (Goodwill and Salvation Army) as the pattern implementation; others follow the same interface

**Frontend**
- Single-page Leaflet map, full viewport
- Cluster markers at low zoom, individual pins at street level
- Pin click: popover with name, org type, address, hours if available, confidence score indicator, upvote/deny buttons
- Turnstile widget renders inline in popover before vote submission
- Minimal submission flow for new locations (name, address, org type — geocoded on submit)
- Zero external UI framework dependencies beyond Leaflet and Turnstile. Vanilla JS or a minimal build step is fine.

**Dev Environment**
- `docker-compose.yml` standing up Postgres/PostGIS + API server + static frontend serve
- `.env.example` with all required variables documented
- `README.md` with setup-to-running-map instructions in under ten steps
- `scripts/seed.sh` that runs the OSM ingest and dedup against the Ohio test region so the map has real data on first boot

### Phase 3 ends when

Running `docker compose up` followed by `bash scripts/seed.sh` produces a working map in the browser with real Ohio donation location data, functional vote buttons, and no console errors.

---

## Phase 4 — Final Validation Pass

**Objective:** Systematic verification that what was built matches what was specified. Not QA theater — actual gap-closing.

1. Walk every endpoint in `ARCHITECTURE.md` and confirm it is implemented, reachable, and returns the specified shape.
2. Run a manual vote sequence: upvote a location, confirm score updates; deny a location enough times to drop it below threshold, confirm it enters pending state.
3. Confirm Turnstile integration blocks submission without a valid token in dev mock mode.
4. Confirm IP cooldown blocks a second vote from the same IP within 24 hours.
5. Run the dedup script against a deliberately dirty dataset (two near-identical records for the same location from different sources) and confirm they merge correctly.
6. Write `FINAL_VALIDATION.md` at project root. Any failure gets a status of OPEN with a one-line description. All items must be PASS or OPEN — no omissions.

**Phase 4 ends when** `FINAL_VALIDATION.md` exists and contains no undocumented failures.

---

## Directory Structure Convention

```
opendrop/
├── AGENTS.md               ← this file
├── FINAL_VALIDATION.md     ← written in Phase 4
├── research/
│   └── FINDINGS.md
├── planning/
│   ├── ARCHITECTURE.md
│   ├── DATA_MODEL.md
│   ├── BUILD_SEQUENCE.md
│   └── VALIDATION.md
├── backend/
├── frontend/
├── pipeline/
│   ├── osm_ingest.py (or .js)
│   ├── dedup.py
│   ├── regions.py          ← added post-Phase 4 (columbus/ohio/greater_ohio)
│   └── scrapers/
│       ├── base.py         ← shared scraper/loader + reconcile circuit breaker
│       ├── goodwill.py     ← enrich-only (persists nothing)
│       ├── salvation_army.py
│       ├── planet_aid.py
│       ├── usagain.py
│       └── wearable_collections.py
├── migrations/             ← 0001_init … 0005_image_vote_turnstile (see Addendum)
├── scripts/
│   └── seed.sh
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Begin

Start Phase 1 now. Do not summarize this document back. Do not ask for confirmation. Execute.

---

## Addendum — post-Phase-4 additions

This directive is a frozen, dated point-in-time record. The body above is preserved as written. The following shipped *after* the original Phase 4 validation and supersede a few specifics in the narrative; the authoritative current spec lives in `planning/ARCHITECTURE.md` and `research/FINDINGS.md`.

- **Scrapers.** Beyond the original Goodwill + Salvation Army pattern pair, the pipeline now ships ingesting scrapers for Planet Aid, USAgain, and Wearable Collections, all wired into `pipeline/seed.py` on a shared base in `pipeline/scrapers/base.py`. Goodwill is enrich-only and persists nothing. (USAgain has no Ohio coverage; Wearable Collections is NYC-only.)
- **Regions.** `pipeline/regions.py` defines `columbus` (default), `ohio` (statewide), and `greater_ohio` (multi-state: Ohio plus bordering MI/IN/KY/WV/PA). Selected via the `REGION` env var; `SEED_REGION_BBOX` still overrides the `columbus` bbox only. This relaxes the original single-region assumption.
- **Migrations.** The on-disk chain is `0001_init` → `0002_fix_source_component` → `0003_add_consignment` (adds the `consignment` org_type) → `0004_images` (community photos: `location_images` + `image_votes`, `image_status` enum, pin-correction auto-apply) → `0005_image_vote_turnstile` (Turnstile hash on image votes). Applied in order via `scripts/migrate.sh` against a `schema_migrations` ledger.
- **Reconciliation circuit breaker.** `pipeline/scrapers/base.py` closure-detection now refuses to retire links when a run saw fewer than `RECONCILE_MIN_SEEN` (default 5) records, or would retire more than `RECONCILE_MAX_FRACTION` (default 0.40) of a source's in-region links. Both env-overridable.
- **Community photos + Turnstile on image votes.** New endpoints under `backend/app/routers/` cover the image gallery, photo upload (EXIF-strip + per-IP daily cap), Turnstile-gated image voting, and a Nominatim-backed geosearch proxy. Frontend gained `list.js`, `photos.js`, and `search.js`.
- **Automated tests.** These additions are now covered by automated tests rather than the manual Phase 4 checklist: `backend/tests/test_api.py` (image-vote and reconcile-breaker cases) and `tests/test_regions.py` (region selection/bbox). No fresh manual validation pass is claimed here.
