# OpenDrop — Phase 2 Validation

> Phase 2 gate (per AGENTS.md): "re-read every Phase 1 finding and confirm each is addressed in the architecture … Any unresolved finding is a blocker. Phase 2 ends when … `VALIDATION.md` contains no open items." This document has two parts: **(A)** Phase 1 finding → architectural resolution, and **(B)** the Phase 2 adversarial design-review results. **Status at bottom: 0 open items.**

---

## Part A — Phase 1 findings → architectural resolution

Source: [research/FINDINGS.md](../research/FINDINGS.md). Every finding is mapped to the doc + section that resolves it.

### A1. OSM Data Audit (Finding 1)

| Finding | Resolution | Where |
|---|---|---|
| Only 3 drop bins in metro; bins under-mapped | Bins come from org feeds (Planet Aid/USAgain) + crowd, not OSM; OSM seeds staffed stores | ARCH §7 / FINDINGS Matrix |
| `collection_times` 0%, `opening_hours` 9.5% | Confidence formula **excludes** OSM hours; `hours` is optional JSONB, populated from org feeds when present | DATA_MODEL §2, §7 / ARCH §9 |
| Structured address only 36% | `address_*` all nullable; geocoding for submissions; tier-2 dedup degrades gracefully when `house_number` NULL | DATA_MODEL §2 / ARCH §7.4 |
| 27 ways + 15 nodes (geometry mix) | OSM ingest uses `out center` to get a point for ways | ARCH §7.1 |
| `name` 90.5%, brand for chains | `name` NOT NULL; `brand_key` canonicalization drives dedup | DATA_MODEL §2 / ARCH §7.4 |

### A2. Org Locator Survey (Finding 2) — ingest vs. enrich/skip honored per org

| Org | Phase 1 verdict | Resolution | Where |
|---|---|---|---|
| Salvation Army | INGEST | `salvation_army.py` (ingest), ZIP-sweep satruck, dedupe on LocationGUID — the **primary stored** scraper | ARCH §7.3 |
| Planet Aid | INGEST | `BaseScraper` (ingest) extension point; grid-sweep, dedupe on id | ARCH §7.2–7.3 |
| USAgain | INGEST | `BaseScraper` (ingest) extension point; zip-sweep HTML, dedupe on lat/lon | ARCH §7.2–7.3 |
| Wearable Collections | INGEST (geocode independently) | `BaseScraper` (ingest); **Nominatim** geocode, never Google `cid` coords | ARCH §7.3 / §9 |
| Goodwill | ENRICH-ONLY (ToS) | `sources.storage_policy='enrich_only'`; scraper is a **pattern demo** that persists nothing (D1) | DATA_MODEL §1, §3 / ARCH §7.3 |
| GreenDrop | SKIP (ToS) | Not a source; optional deep-link only | FINDINGS Matrix |
| DAV | SKIP (no dataset) | Not a source | FINDINGS Matrix |

### A3. Existing Aggregator Audit (Finding 3)

| Tool | Verdict | Resolution | Where |
|---|---|---|---|
| clothedonations.com | Reference only (D2) | **Not a source**; out-of-band coverage QA benchmark | ARCH §9 |
| earth911.com | Enrichment-only if key+license | Not in v1 system | FINDINGS §3 |
| donationtown.org | Skip | Not in system | FINDINGS §3 |

### A4. Deduplication Scope (Finding 4)

| Finding | Resolution | Where |
|---|---|---|
| Validated predicate `≤300 m AND name-sim ≥0.4` (0 FP) | Implemented as the primary branch | ARCH §7.4 / DATA_MODEL §7.5 |
| Brand canonicalization is load-bearing | `brand_key` column + canonicalization map; `brand_equal` defined | DATA_MODEL §2 / ARCH §7.4 |
| Tier-2 `≤600 m + street#` recovers the 1 FN | `house_number` column + `normalize_house_number`, **retains** name-sim ≥0.4 | ARCH §7.4 |
| Distance collides distinct co-located stores | name-sim tie-break + tight unbranded-bin path (≤25 m) | ARCH §7.4 |

### A5. Stack Validation (Finding 5)

| Component | Resolution | Where |
|---|---|---|
| PG16 → 17 (D3) | PostgreSQL 17 + `postgis/postgis:17-3.5` (no 3.6-only feature; verified-existing tag) | DATA_MODEL header / ARCH §8 |
| PostGIS 3.6.x | 3.6 is a no-impact future bump; ship 3.5 image | FINDINGS Finding 5 / ARCH §8 |
| Leaflet 1.9.4 | Pinned exact; markercluster 1.5.3 exact | ARCH §5 |
| Turnstile free, siteverify, test keys | Dev-mock via CF test secret; server re-verifies; single-use is prod-only (documented) | ARCH §6 |
| Overpass batch-only (⚠) | API **never** proxies Overpass; only `osm_ingest.py` does, offline; serve from PostGIS | ARCH §1, §7.1, §9 |

### A6. Decisions D1/D2/D3 and Open Items 1–5

| Item | Resolution | Where |
|---|---|---|
| D1 Goodwill enrich-only | Loader `storage_policy` branch (primary gate) + field-provenance invariant + `v_public_locations` backstop | DATA_MODEL §2, §3, §9 / ARCH §7.2–7.3, §9 |
| D2 clothedonations not stored | Excluded from system; QA reference only | ARCH §9 |
| D3 PG16→17 | PG17 across all docs | ARCH §3, §8 |
| Open-1: confidence ⊥ OSM hours | Formula = source authority + crowd + staleness only | DATA_MODEL §7 |
| Open-2: storage policy first-class | `sources.storage_policy` + per-row `is_redistributable` + field-provenance invariant | DATA_MODEL §1, §2 |
| Open-3: ingest = sweep+dedupe | Shared `loader` + `BaseScraper`, not bespoke loaders | ARCH §7.2 |
| Open-4: redistributable geocoder | Nominatim; never store Google geometry | ARCH §5, §7.3, §9 |
| Open-5: Overpass batch-only | Enforced (A5) | ARCH §1, §7.1 |

---

## Part B — Phase 2 adversarial design review (7 dimensions)

A 7-agent review stress-tested the draft spec (SQL correctness, API/schema/frontend consistency, dedup/confidence soundness, abuse model, redistribution leak-tracing, directive coverage, deployability with live registry checks). One lens (api_schema_frontend_consistency) returned **sound**; the others surfaced **2 blockers + ~12 majors**, all now resolved. Resolutions:

### Blockers

| # | Issue | Resolution |
|---|---|---|
| B-1 | Multi-source (deduped) locations could never be denied below threshold — crowd floored at −40 but `source_component` reaches 85, so a 2-source row stays active forever (breaks Phase-4 deny test on real seed data). | Added **deny-dominance override** to `recompute_confidence()`: `denies≥5 AND denies≥confirms+5` floors confidence ≤20 → `pending`, regardless of source authority. Worked example now proves both single- and multi-source cases. (DATA_MODEL §7) |
| B-2 | Crowd-submission **promotion path had no build task** — `awaiting→promoted` never happened; `crowd` source, `promoted_location_id`, `pending_status` transitions all dead; submissions never reached the map. | Added **§7.6 promotion** (auto on submit when geocoded+non-dup; standalone `pipeline/promote.py`) and **BUILD task D7**; fixed the dangling DATA_MODEL §5 cross-reference. (ARCH §7.6 / BUILD D7 / DATA_MODEL §5) |

### Majors

| Issue | Resolution |
|---|---|
| `trg_after_source` pinned `last_verified_at` to 1970 epoch when no sources → phantom −20 staleness | Removed `to_timestamp(0)` sentinel; `GREATEST` NULL-skips, keeps prior value (DATA_MODEL §7) |
| Worked example assumed fresh; staleness could mask an upvote | Stated `last_verified_at=now()` precondition; documented that a confirm refreshes staleness so score always rises; corrected "5 denies"→4 (DATA_MODEL §7) |
| `brand_equal` undefined; unsound for unbranded bins (empty-name sim=1.0 trap) | Defined `brand_key`; two NULL brands never equal; separate tight (≤25 m) unbranded-bin path (DATA_MODEL §2 / ARCH §7.4) |
| Dedup "idempotent" unbacked; merged tombstones re-evaluated; A→B→C chains | Candidate gen excludes `merged/hidden`; merge chain-compacts `merged_into_id` + resets loser counters (ARCH §7.4) |
| `street_number_equal` undefined/non-executable; tier-2 dropped name-sim vs validated report | `normalize_house_number`; NULL⇒non-match; **restored** name-sim ≥0.4 in tier-2; reconciled FINDINGS/ARCH/BUILD (ARCH §7.4 / FINDINGS Finding 4) |
| IP cooldown race (SELECT-then-INSERT under READ COMMITTED) | `pg_advisory_xact_lock(hashtext(location_id||ip_hash))` per vote txn (ARCH §4.3, §6) |
| Left-most `X-Forwarded-For` spoofable → cooldown bypass | Trust `X-Real-IP` set by nginx (`$remote_addr`, overwritten); ignore inbound XFF (ARCH §6, §8) |
| D1 wording implied query-time enrichment that doesn't exist | Reframed Goodwill as **v1 pattern-demo** (fetch+dedup+log, no persist/surface); query-time enrichment = future (FINDINGS D1 / ARCH §7.3) |
| Multi-source field precedence undefined; field-level taint possible | **Field-provenance invariant**: canonical columns only from ingest sources by authority; loader/merge enforce; tested (DATA_MODEL §2 / ARCH §7.4) |
| Export attribution only in HTTP header → ODbL lost on save | Attribution + license embedded as **in-payload** FeatureCollection members (ARCH §4.6) |
| `migrate.sh` idempotency impossible (`CREATE TYPE` has no `IF NOT EXISTS`) | **`schema_migrations` ledger**; DDL applied once, re-runs skipped (DATA_MODEL header / ARCH §8 / BUILD B2) |
| PostGIS 3.5 vs 3.6 drift; stale "bump when published" comment; 3.6.4 not a release | Reconciled to **3.5 image** (verified) across all docs; corrected 3.6.4→3.6.2; no 3.6-only feature (ARCH §8 / FINDINGS Finding 5 / DATA_MODEL header) |
| No host port mapping; unexplained CORS origin | Added `WEB_PORT`/`ports ['8080:80']`; noted CORS no-op in same-origin nginx (ARCH §8) |
| `pg_isready` healthcheck races initdb temp-server window | Schema-aware healthcheck (`select 1 from sources`) + api startup retry/backoff (ARCH §8) |

### Minors / nits (all applied)

`bool_or` tautology → `COUNT(*)>0`; `state char(2)`→`varchar(2)`; merged-row 404 shape pinned (`error.details.canonical_id`); detail endpoint serves `pending` rows (recovery path for denied locations); example brands switched from Goodwill→Salvation Army for stored rows; `attribution` shown on all source examples; `ST_ClusterDBSCAN`→`ST_SnapToGrid` in §2; trgm-index role clarified (API submit pre-filter, not batch sim); `/api/meta` sources filtered to ingest-contributing; §9 reworded so the loader branch is named the primary D1 gate; markercluster pinned 1.5.3; Turnstile dev-mock replay caveat documented; `4 denies` correction.

---

## Open items

**None.** All Phase 1 findings are mapped to an architectural resolution (Part A), and all Phase 2 review blockers/majors/minors are resolved in the planning docs (Part B). 

**Phase 2 is closed. Construction (Phase 3) may begin from [BUILD_SEQUENCE.md](BUILD_SEQUENCE.md).**
