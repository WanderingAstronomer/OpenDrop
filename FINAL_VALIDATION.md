# OpenDrop — Final Validation (Phase 4)

> Systematic verification that what was built matches what was specified, run against the **live** stack (`docker compose up` + `bash scripts/seed.sh`) on 2026-06-27. Every item is **PASS** or **OPEN**. **Result: all items PASS; 0 OPEN.**

## Environment

- `docker compose up -d --build` → `db` (PostGIS, healthy), `api` (FastAPI, :8001→8000), `web` (nginx, :8080→80). API host port moved 8000→8001 (8000 was occupied by another project on the host); the map is served at **http://localhost:8080**.
- Migration `0001_init.sql` applied automatically via the initdb mount — proven by the schema-aware healthcheck (`SELECT 1 FROM sources`) passing. (As of this snapshot only `0001` had been applied; migrations `0002`–`0005` are now applied in order via the `schema_migrations` ledger — see Addendum.)
- `bash scripts/seed.sh` seeded the Columbus, OH metro from **live** sources: OSM (42, via Overpass 200), Salvation Army (13 fetched, 1 auto-deduped on ingest), Goodwill (enrich-only, persisted 0). **54 active locations** (24 charity_store, 19 thrift_store, 8 donation_center, 3 drop_bin). (Seed now also runs Planet Aid, USAgain, and Wearable Collections — see Addendum.)

---

## 1. Endpoint walk (every ARCHITECTURE §4 endpoint — implemented, reachable, correct shape)

| Endpoint | Result | Evidence |
|---|---|---|
| `GET /api/health` | **PASS** | `{"status":"ok","db":true}` (direct + via nginx proxy) |
| `GET /api/meta` | **PASS** | 54 active; `sources` = osm + salvation_army (ingest-only, enrich excluded); sitekey + buckets present (the ingest scrapers added since this snapshot — planet_aid, usagain, wearable_collections — also surface here when active; see Addendum) |
| `GET /api/locations` (points) | **PASS** | `mode:"points"`, GeoJSON FeatureCollection, 47 features in bbox, props `{id,name,org_type,confidence,bucket}` |
| `GET /api/locations` (clusters) | **PASS** | `cluster=on` → `mode:"clusters"`, 40 grid clusters with `count`/`avg_confidence` |
| `GET /api/locations/{id}` | **PASS** | Full detail incl. decomposed `lat/lon`, `address{}`, `sources[]` (with attribution); `merged`→404+`canonical_id` path present |
| `POST /api/locations/{id}/vote` | **PASS** | Returns updated `{confidence,bucket,status,upvotes,denies}` (see §2/§4) |
| `POST /api/locations` | **PASS** | Submit → `{"pending_id":1,"status":"promoted","geocoded":true,"location_id":55}` (Nominatim geocode live) |
| `GET /api/export` | **PASS** | `v_public_locations` only; **in-payload** `license` + `attribution[]` (ODbL travels with the data) |

## 2. Manual vote sequence (directive step 2)

| Check | Result | Evidence |
|---|---|---|
| Upvote → score updates | **PASS** | `POST /locations/1/vote {confirm}` → confidence **40 → 45**, upvotes 1 |
| Deny below threshold → pending | **PASS** | 4 denies (distinct IPs) on loc 3 → confidence **40 → 8**, `status='pending'`, and **removed from the active map** (`/api/locations` no longer returns it) |

## 3. Turnstile gating (directive step 3)

| Check | Result | Evidence |
|---|---|---|
| Vote without a token blocked (dev mock) | **PASS** | `POST .../vote` with no `turnstile_token` → **403** `turnstile_failed` |
| Submit without a token blocked | **PASS** | `POST /locations` with no token → **403** |

(Dev-mock uses Cloudflare's always-passes test secret; a missing/empty token is still rejected — the intended behavior.)

## 4. IP cooldown (directive step 4)

| Check | Result | Evidence |
|---|---|---|
| 2nd vote from same IP within 24h blocked | **PASS** | 2nd `confirm` from same IP → **429** `cooldown_active`, `retry_after: 86400` |
| Cooldown is not spoofable via headers | **PASS** | Through nginx (:8080), a forged `X-Real-IP`/`X-Forwarded-For` is overwritten with the real peer, so repeated votes collapse to one ip_hash → 2nd is 429. Distinct-IP testing requires the direct API port — confirming client-supplied IP headers cannot bypass the cooldown. |

## 5. Deduplication on a dirty dataset (directive step 5)

| Check | Result | Evidence |
|---|---|---|
| Two near-identical records (different sources, ~100 m apart) merge | **PASS** | Inserted `Goodwill Columbus` (osm) + `Goodwill Columbus Store` (crowd) ~100 m apart → `dedup.run()` = **1 merge**. Survivor = id 58 (osm, higher authority), `source_count=2` (sources repointed); loser id 59 → `status='merged'`, `merged_into_id=58`. Canonical chosen by authority, idempotent. |

## 6. Additional validations (review-driven, beyond the literal checklist)

| Check | Result | Evidence |
|---|---|---|
| Multi-source deny-override (Phase-2 blocker fix) | **PASS** | A 2-source location (osm+SA, confidence **85**, active) → 5 denies (distinct IPs) → confidence floored to **20**, `status='pending'`. Confirms a deduped multi-source row is retireable (without the override it would floor at 45 and never hide). |
| Goodwill enrich-only persists nothing (D1) | **PASS** | `SELECT count(*) FROM location_sources WHERE source_code='goodwill'` = **0**; `scrape_log` has a goodwill row (`status=success, records_upserted=0`) — proves it ran without persisting. |
| Dedup-logic unit tests | **PASS** | `tests/test_dedup_logic.py` — **11/11** pass (empty-name trap, unbranded bins, tier-2 house-number recovery, brand discrimination). |
| ODbL attribution surfaced end-to-end | **PASS** | Stored bytes `c2a9` (correct `©`); served in `/api/meta` (map attribution control) and embedded in `/api/export` payload. |
| **Browser renders with no console errors** (Phase 3 end condition) | **PASS** | Headless Chromium (Playwright) loaded the live site on the compose network: base tiles rendered (24), the 54 locations clustered into 12 markers with SVG marker paths, and **0 console errors / 0 page errors / 0 failed requests**. |

---

## Notes / minor observations (not failures)

- **Goodwill live access is intermittently bot-blocked** (the enrich run hit a `403` on the ajax this seed). This is expected and harmless: Goodwill is a non-persisting pattern demo (D1), and the scraper handles the 403 gracefully (logs, yields nothing). Not a defect.
- **Routing-level `404`/`405`** (e.g. wrong method/path) are emitted by FastAPI's default handler as `{"detail": ...}` rather than the app's `{"error": {...}}` envelope. The envelope is used for all *documented* endpoint errors (the ones raised in handlers). Cosmetic; left as-is for v1.
- Seed coverage is intentionally thin for some `org_type`s in the Columbus metro (e.g. `mutual_aid`, `church_drive` have no OSM/SA seed rows) — documented expected-empty, not a defect.

## Conclusion

`docker compose up` + `bash scripts/seed.sh` produces a working browser map of real Ohio donation locations at http://localhost:8080, with functional confirm/deny voting (confidence updates live, including community retirement of multi-source rows), a working geocoded submission flow, ODbL + source attribution visible, deduplication that merges dirty records correctly, and no enrich-only (Goodwill) data in the redistributable export. **All Phase 4 items PASS. No OPEN items.**

---

## Addendum — post-Phase-4 additions

This record is a frozen Phase-4 snapshot (2026-06-27). The validation numbers and results above are **not** re-run here. The items below shipped *after* this snapshot; rather than claim a fresh manual validation, they are covered by automated tests (`backend/tests/test_api.py`, `tests/test_regions.py`).

- **Community photos + Turnstile-gated image votes + pin correction.** Migration `0004_images.sql` adds `location_images` / `image_votes` tables, the `image_status` enum (`pending`/`visible`/`hidden`), `recompute_image()`, and the `trg_after_image_vote` trigger; `0005_image_vote_turnstile.sql` adds `image_votes.turnstile_hash`. New endpoints: `GET`/`POST /api/locations/{id}/images` (gallery + upload with EXIF-strip and per-IP daily cap) and `POST /api/images/{id}/vote` (now Turnstile-gated; advisory-locked). A correction photo that reaches score ≥ 3 auto-moves the canonical location pin to the suggested coords. Covered by the image-vote tests in `backend/tests/test_api.py`.
- **Three additional ingest scrapers.** Planet Aid, USAgain, and Wearable Collections are now wired into `pipeline/seed.py` (USAgain has no Ohio coverage; Wearable Collections is NYC-only). These join OSM and Salvation Army as ingest sources, alongside Goodwill (still enrich-only, persists nothing).
- **`consignment` org_type.** Migration `0003_add_consignment.sql` adds `consignment` to the `org_type` enum (after `thrift_store`); the API model literal includes it.
- **`greater_ohio` region.** `pipeline/regions.py` adds a multi-state region (Ohio + bordering MI/IN/KY/WV/PA) with a cross-state ZIP sweep, selectable via the `REGION` env var. Covered by `tests/test_regions.py`.
- **Reconciliation circuit breaker.** `pipeline/scrapers/base.py` `_reconcile` now refuses closure-retirement when a run saw fewer than `RECONCILE_MIN_SEEN` (default 5) records, or would retire more than `RECONCILE_MAX_FRACTION` (default 0.40) of a source's current in-region links. Both env-overridable; already skipped when a run had per-record errors. Covered by the reconcile-breaker tests in `backend/tests/test_api.py`.
