# OpenDrop — Phase 1 Findings

> **Status:** ✅ Phase 1 complete. All findings below were **verified against live sources on 2026-06-27** (Overpass pull, org-locator probes, aggregator fetches, stack release pages). No claim rests on training-data memory. Raw evidence lives in [`research/data/`](data/).

---

## GO / NO-GO

**GO.** The core thesis holds with hard evidence: no authoritative, redistributable aggregator of US clothing-donation locations exists, and the available data is both **scattered** (each org runs its own incompatible locator) and **stale/sparse** (OSM has near-zero drop-bin and hours coverage; the one strong aggregator, clothedonations.com, has no per-record freshness signal). At the same time, **enough cleanly-redistributable first-party data exists to seed a real map today** — The Salvation Army, Planet Aid, USAgain, and OSM are all ingestible without auth, and a working deduplication predicate (`brand-equal AND ≤300 m AND name-sim ≥0.4`) was validated end-to-end on real Columbus data at **0 false positives / 1 false negative**. The stack (PostGIS 3.6 on PostgreSQL, Leaflet 1.9.4, Cloudflare Turnstile, Overpass-as-batch-source) is current and fit-for-purpose with one version bump (PG 16 → 17) and one usage constraint (never proxy Overpass at request time). Proceed to Phase 2.

---

## Decisions made under the Escalation Policy

The directive authorizes autonomous calls with inline documentation. Three findings touched planning assumptions; each is resolved here and carried into Phase 2 rather than blocking:

| # | Finding | Assumption affected | Decision |
|---|---------|--------------------|----------|
| D1 | **Goodwill's Terms of Use explicitly forbid scraping and reproduction** (technically scrapeable, legally not redistributable). | AGENTS.md Phase 3 names Goodwill as one of the two pattern scrapers. | **Build the Goodwill scraper anyway** as the pattern implementation, tagged `storage_policy = enrich_only`. For **v1 it is a scraper-interface pattern demo only**: it fetches + dedup-matches, writes a `scrape_log` audit row, and **persists nothing** to the canonical/redistributable dataset (no `location_sources`, no surfaced data). Salvation Army is the primary *stored* scraper. *(Live query-time enrichment — merging Goodwill fields into a response without storing — is a documented **future extension**, out of scope for v1.)* The directive's intent (demonstrate the scraper interface on two real orgs; treat Goodwill as enrichment-only / never store) is satisfied without violating "all stored data must be redistributable." |
| D2 | **clothedonations.com exposes its entire 14,655-record geocoded dataset as one open JSON file** — by far the richest single source — but it is a *proprietary aggregation* with no open license. | A naive read says "ingest the biggest dataset." | **Do not store it verbatim.** Use it as a **coverage-validation/QA reference** only (compare our canonical counts against it per state). Build the canonical store from clean first-party sources (OSM + Salvation Army + Planet Aid + USAgain). Revisit only if an explicit license/permission is obtained. |
| D3 | **PostgreSQL 16 is no longer the newest** (18.4 is current; 19 in beta). 16 is still supported through Nov 2028. | Directive assumes PG 16. | **Bump to PostgreSQL 17** (most battle-tested major with a long support runway, EOL 2029-11). PostGIS 3.6.x supports it; deployed image ships PostGIS 3.5 (see Finding 5). Non-blocking, low-risk. |

---

## Finding 1 — OSM Data Audit (Columbus, OH metro)

**Method:** Overpass query ([`data/columbus_query.overpassql`](data/columbus_query.overpassql)) over bbox `39.80,-83.25,40.18,-82.75`, analyzed by [`data/analyze_osm.py`](data/analyze_osm.py). Raw result: [`data/osm_columbus.json`](data/osm_columbus.json) (42 features).

| Feature class | Count |
|---|---:|
| `shop=charity` | 20 |
| `shop=second_hand` | 19 |
| `amenity=recycling` + `recycling:clothes=yes` (drop bins) | 3 |
| **Total** | **42** |

**Tag completeness (share of all 42 features):**

| Tag | Coverage | Implication |
|---|---:|---|
| `name` | 90.5% | Usable identifier for most records |
| `addr:*` (any) | 35.7% | Most records have **no structured address** → geocoding/normalization needed |
| `opening_hours` | 9.5% | Hours essentially absent |
| **`collection_times`** | **0.0%** | The directive's target tag **does not exist anywhere in the region** |
| `operator` / `brand` | 14.3% / 35.7% | Brand present for chains (Goodwill ×11, Salvation Army ×3); load-bearing for dedup |
| `website` / `phone` | 11.9% / 9.5% | Contact data sparse |

**Schema gaps & conclusions:**
- **Drop bins are catastrophically under-mapped** — only 3 clothing-recycling bins for an entire metro, vs. real-world counts in the hundreds (cf. Planet Aid/USAgain footprints). OSM is a decent seed for *staffed stores*, not bins.
- **`collection_times` is a dead tag here** — confidence/freshness cannot lean on it; OpenDrop must carry its own hours + verification fields.
- **Geometry mix:** 27 ways (mapped as building polygons) + 15 nodes → ingest must use `out center` to get a point for ways (done).
- This is exactly the gap crowd-validation exists to fill: OSM gives a sparse, name-rich skeleton; freshness and bins come from org feeds + crowd votes.

---

## Finding 2 — Org Locator API Survey (7 orgs)

Every endpoint below was **probed live**. "Ingest" = first-party data, no auth, no prohibiting ToS → storable with attribution. "Enrich-only/skip" = legal (ToS) or data-availability blocker.

| Org | Endpoint type | Auth | Scrapeable | Has lat/lon | Has hours | ToS verdict | **Rec.** |
|---|---|---|---|---|---|---|---|
| **Salvation Army** | JSON API (`satruck.org/apiservices/pickup/donategoods/locations`) | none | yes | ✅ | ✅ (free-text) | robots allow-all, no prohibiting ToS | **INGEST** |
| **Planet Aid** | JSON API (`api.binlocator.planetaid.org/AzureSearch/sites`) | none | yes | ✅ | ❌ | no ToS/robots restriction | **INGEST** |
| **USAgain** | Server-rendered HTML (`usagain.com/find-treemachine?zip=`) | none | yes | ✅ (in markers) | ❌ (24/7 bins) | no robots, no ToS | **INGEST** |
| **Wearable Collections** | HTML (Squarespace `/greenmarket`) | none | yes | ❌ (Google `cid` only) | ✅ (day/hours) | robots allow | **INGEST** (geocode independently) |
| **Goodwill** | JSON API (WP `admin-ajax gwlf_get_locations`) | none (nonce harvestable) | yes | ✅ | ✅ (2nd call) | **ToS forbids scraping + reproduction** | **ENRICH-ONLY** (see D1) |
| **GreenDrop** | HTML + JSON-LD, sitemap-enumerable (88 centers) | none | yes | ✅ | ✅ | **ToS forbids crawling + redistribution** (TVI/Savers proprietary) | **SKIP** (link-only) |
| **DAV** | Fragmented; no national locator | — | partial | only 1 regional list (8 VA stores) | — | n/a | **SKIP** |

**Key per-org specifics for Phase 2/3:**
- **Salvation Army** — the cleanest ingest. `GET …/donategoods/locations?Type=3&ZipCode=NNNNN&otid=0` returns `Name, Address1/2, City, State, Zip, Latitude, Longitude, Hours, ContactPhone, Website, stable Id + LocationGUID, TypeName (DROPOFF/STORE/ARC)`. No bulk endpoint → **ZIP-centroid sweep + dedupe on `LocationGUID`**. Coverage weakest in NW (Seattle/Portland returned 0) → backfill from OSM. Hours/TypeName are free-text → normalize.
- **Planet Aid** — `GET …/AzureSearch/sites?latitude=&longitude=` returns top-20 nearest: `id, geoPoint{lat,lng}, siteName, siteAddress (single string), siteTypeCode/Id`. **No hours/phone.** ~10k bins across ~14 states (Mid-Atlantic/Midwest/NE). → **lat/lng grid sweep + dedupe on `id`**; parse the combined address string ourselves. *(now implemented — ingesting scraper.)*
- **USAgain** — returns the **10 nearest** bins per zip as HTML with `new google.maps.LatLng(...)` markers (lat/lon are USAgain's own, not Google-licensed). 15 states only. All unattended 24/7 drop-off → set `drop_off=true`, attach a global accepted-items list. → **zip-sweep + dedupe on lat/lon**. *(now implemented — ingesting scraper; no Ohio coverage in practice.)*
- **Wearable Collections** — ~8 NYC GrowNYC greenmarket sites; **no coordinates in HTML** (only Google `cid` links, which are *not* redistributable). → store name/day/hours, **geocode via OSM/Nominatim or match GrowNYC**, not Google. Tiny → could be hand-curated. *(now implemented — ingesting scraper; NYC-only.)*
- **Goodwill** — donation sites cleanly separable via `cats=1` / `ci_servD` flag; 100-row/query cap → geo-tiling; nonce rotates (re-harvest from page). Excellent data, blocked only by ToS → enrich-only path (D1). *Independent regional Goodwills (e.g. goodwillcolumbus.org) may publish under different terms — a future per-region opt-in.*
- **GreenDrop / DAV** — documented and parked. GreenDrop's data is excellent but ToS-blocked; DAV has effectively no national dataset (and note: **pickupplease.org is Vietnam Veterans of America, not a DAV partner** — corrects the directive's premise).

---

## Finding 3 — Existing Aggregator Audit

| Tool | Feed/API | Worth ingesting? | Verdict |
|---|---|---|---|
| **clothedonations.com** | **Open static JSON** `https://www.clothedonations.com/data/map-locations.json` — 3.67 MB, **14,655 pre-geocoded records**, 2,627 orgs, all 50 states, `{lat,lng,company,city,address,state,slug,phone}` | Technically trivial | **Reference/QA only** (D2) — proprietary aggregation, no license, no per-record freshness. Best **coverage benchmark** we have; not a stored source. |
| **earth911.com** | Real REST API (`api.earth911.com`) w/ proximity search, ~1.6M locations | Partial | **Enrichment only, if a key + license is obtained.** Request-only `api_key` (401 without), it's a *recycling* DB (clothing is a `material_id` subset), and robots blocks AI crawlers. |
| **donationtown.org** | None | No | **Skip.** Abandoned its 2008-era charity directory; now a lead-gen funnel to pickupplease.org. Useful only as a competitive baseline. |

**Methodology signal:** the only broad dataset (clothedonations) is a one-time scrape/geocode of chain directories + regional charities with no freshness mechanism — precisely the staleness failure mode OpenDrop's confidence scoring is designed to beat. Its company histogram (Goodwill 2,926 / Planet Aid 2,568 / Salvation Army 1,739 …) also confirms our first-party ingest targets are the high-volume backbone.

---

## Finding 4 — Deduplication Problem Scope (validated on real data)

**Method:** Live-fetched **33 Columbus Goodwill** locations from goodwill.org's locator (Set B) and paired them against the **42 OSM** features (Set A) using a stdlib harness — haversine distance + (`difflib` ratio ∪ token-set Jaccard) name similarity + **brand canonicalization**. Swept distance {50,100,150,300,500 m} × name-sim {0.4,0.6,0.8}. Artifacts: [`dedup_sample.py`](data/dedup_sample.py), [`dedup_candidates.json`](data/dedup_candidates.json), [`dedup_sample_report.md`](data/dedup_sample_report.md).

**Results (ground-truth adjudicated):** 10 true duplicates exist. The **`≤300 m AND name-sim ≥0.4`** gate caught **9 — with 0 false positives and 1 false negative**.

**Critical empirical findings → directly shape the merge algorithm:**
1. **Brand canonicalization is load-bearing.** Once names normalize to a brand token (Goodwill↔Goodwill = 1.00; non-matches ≤0.24), there is a wide empty band — so *any* name-sim cut in [0.4, 0.8] behaves identically. **Distance does all the discrimination among same-brand pairs.**
2. **The name gate must still exist for precision.** At a distance-only 300 m gate, a Volunteers-of-America Thrift (240 m, name-sim 0.14) and a "One More Time" shop (279 m, name-sim 0.24) sit near a Goodwill and would be wrongly merged. The ≥0.4 gate rejects both → 0 FP.
3. **The lone false negative** is a same store geocoded 513.8 m apart (OSM node vs. feed). → add a **tier-2 rule: `≤600 m AND brand-equal AND street-number match`** rather than widening the global radius (which would re-introduce FPs).
4. **Distance alone collides distinct stores** — the Goodwill feed itself had two different stores sharing one coordinate (Reynoldsburg vs. Brice Rd Outlet) → name-sim is the necessary tie-breaker on messy feeds.

**Recommended OpenDrop merge predicate (→ DATA_MODEL/dedup spec):**
```
match(a, b) :=
   brand_equal(a, b)
   AND ( (haversine(a,b) ≤ 300m AND name_sim(a,b) ≥ 0.4)
         OR (haversine(a,b) ≤ 600m AND name_sim(a,b) ≥ 0.4 AND street_number_equal(a,b)) )
   -- (separate path for unbranded co-located bins: both brand_key NULL AND same org_type AND ≤25m)
```
with name normalization = lowercase → strip ®/™ & non-alphanumerics → remove noise phrases ("donation center", "thrift store", "outlet", …) and street-type tokens → **canonicalize known brands** (Goodwill / Salvation Army / Volunteers of America / Habitat / …) to a single token.

---

## Finding 5 — Stack Validation

| Component | Directive assumed | Verified current (2026-06-27) | Verdict | Action |
|---|---|---|---|---|
| **PostgreSQL** | 16 | 18.4 stable (19 beta). 16 supported to 2028-11. | update | **Use PG 17** (EOL 2029-11; battle-tested). |
| **PostGIS** | 3.x | **3.6.2** latest stable upstream (supports PG 12–18) | ✅ ok | Schema uses no 3.6-only feature; the official **non-alpine `postgis/postgis:17` image ships PostGIS 3.5** (3.6 non-alpine not yet published; `17-3.6-alpine` exists). Pin **`17-3.5`**; 3.6 is a no-impact future bump. |
| **Leaflet.js** | latest | **1.9.4** is still latest stable (2.0 alpha-only, ESM/breaking) | ✅ ok | Pin `^1.9.4`; prefer ESM-friendly patterns for future 2.0. |
| **Cloudflare Turnstile** | free CAPTCHA | Confirmed free: 20 widgets/acct, 10 hostnames/widget, **unlimited verifications**. Tokens 300 s, single-use, ≤2048 chars. siteverify: `POST challenges.cloudflare.com/turnstile/v0/siteverify`. | ✅ ok | Managed mode; validate every token server-side with `remoteip`; treat single-use. |
| **Overpass API** | public `overpass-api.de` | Live; ~2 slots/IP; fair use **<10k req/day, <1 GB/day**, 180 s default timeout. | ⚠️ caution | **Batch-import only**, cache into PostGIS, **never proxy live user queries**. Set explicit `[out:json][timeout:]` + descriptive User-Agent. Self-host if ETL volume grows. |

---

## Redistributability Matrix (synthesis → governs Phase 2 source policy)

This is the single most important architectural input from Phase 1.

| Source | Stored in canonical (redistributable) DB? | Role |
|---|---|---|
| **OpenStreetMap** | ✅ yes (ODbL — **must attribute**) | Primary seed for staffed stores |
| **Salvation Army (satruck)** | ✅ yes (first-party, attribute) | Primary stored scraper (drop-offs + ARC) |
| **Planet Aid** | ✅ yes (first-party, attribute) | Bin coverage (Mid-Atlantic/Midwest/NE) |
| **USAgain** | ✅ yes (first-party, attribute, polite rate) | Bin coverage (15 states) |
| **Wearable Collections** | ✅ yes (geocode independently — **not** Google coords) | NYC greenmarket bins |
| **Goodwill** | ⛔ no — **enrich/validate at query time only** (ToS) | Pattern scraper; non-stored |
| **GreenDrop** | ⛔ no (ToS) | Optional deep-link only |
| **clothedonations.com** | ⛔ no (license unclear) | Coverage QA benchmark |
| **earth911** | ⛔ no unless key+license | Possible future enrichment |
| **Google Places / Foursquare** | ⛔ no (per directive) | Query-time enrichment only |

Every stored row will carry a `source` + `license`/`storage_policy` flag so the redistributable export can be filtered by policy.

---

## Open items carried to Phase 2

1. **Confidence-score formula** must not depend on `collection_times`/`opening_hours` from OSM (absent). Base it on: source count/agreement, source authority weight, crowd votes, and age-since-last-verification.
2. **Source storage policy** (`ingest` vs `enrich_only`) must be a first-class field on `location_sources` (drives D1/D2).
3. **Ingest pattern is "ZIP/geo sweep + dedupe"** for every org (none expose a bulk endpoint) — architect a shared sweep+dedupe harness, not bespoke per-org loaders.
4. **Geocoding dependency** for sources without coordinates (Wearable Collections; some OSM addr-only) — pick a redistributable geocoder (Nominatim/OSM), never store Google-derived geometry.
5. **Overpass is batch-only** — the live API serves from PostGIS exclusively.

## Data artifacts (committed under `research/data/`)

- `columbus_query.overpassql`, `osm_columbus.json`, `osm_columbus_flat.json`, `analyze_osm.py` — OSM audit
- `org_feed_columbus.json` — representative live Goodwill sample (subset of the 33 fetched)
- `dedup_sample.py`, `dedup_candidates.json`, `dedup_sample_report.md` — dedup validation

---

## Addendum — post-Phase additions

*This section is appended after the frozen Phase-1 record above. It does not re-run or revise the Phase-1 validation; it only notes what shipped afterward so this document does not mislead about the live system. See the named automated tests for verification rather than fresh manual checks.*

- **Scrapers built per the recommendations:** Salvation Army, Planet Aid, USAgain, and Wearable Collections all ship as **ingesting** scrapers, plus OSM ingest and a Goodwill **enrich-only** scraper that persists nothing (matching D1). All are wired through `pipeline/seed.py` over a shared sweep+dedupe base (`pipeline/scrapers/base.py`). In practice USAgain has no Ohio coverage and Wearable Collections is NYC-only.
- **Regions:** `pipeline/regions.py` defines `columbus` (default), `ohio` (statewide), and a newer `greater_ohio` multi-state region (Ohio + bordering MI/IN/KY/WV/PA) with a cross-state ZIP sweep list. Region selection is via the `REGION` env var; see `tests/test_regions.py`.
- **Reconciliation circuit breaker:** the closure-detection path in `base.py` now refuses to retire source links when a run saw too few records (`RECONCILE_MIN_SEEN`, default 5) or would retire too large a fraction of a source's in-region links (`RECONCILE_MAX_FRACTION`, default 0.40); both env-overridable. Covered by the reconcile-breaker tests in `backend/tests/test_api.py`.
- **Community photos + pin corrections:** later migrations add `location_images` / `image_votes` tables, an upload + helpful/unhelpful vote flow (Cloudflare Turnstile-gated, EXIF-stripped), and an auto-apply of a suggested pin correction once a correction photo reaches a vote score threshold. Image-vote behavior is covered by tests in `backend/tests/test_api.py`.
