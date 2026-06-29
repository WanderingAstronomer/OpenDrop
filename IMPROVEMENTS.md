# OpenDrop — Improvement Roadmap

> Output of a 7-dimension project-wide scan (frontend/UX/a11y, backend API, pipeline/data, security/abuse, ops/deploy, tests/CI, data/civic-licensing). The system is **working and validated** (Phases 1–4); this is the path from "validated demo" to "production civic infrastructure." Items are tagged **impact** (H/M/L) and **effort** (S/M/L), grouped by priority. Cross-cutting themes flagged by multiple reviewers are called out.

## ✅ Shipped since this scan

- **0.1 AGPL-3.0 LICENSE** (+ ODbL dataset / CC0-submissions documented).
- **0.2 CI is real** — GitHub Actions (postgis service, ruff, all migrations, `pytest`), unified `pytest.ini`, `requirements-dev.txt`, Makefile. 50/50 tests pass.
- **0.3 Scheduled re-sync** — `pipeline/sync.py` + opt-in `scheduler` compose profile; **+ closure/deletion detection** in the loader (region-scoped). *(Surfaced and fixed a real `LEAST(85,NULL)=85` confidence bug — migration 0002 + regression test.)*
- **0.4 Secrets fail-fast guard** (`APP_ENV=prod` refuses default salt/Turnstile-test-key/DB password).
- **0.6 nginx security headers + gzip**; **read rate-limiting** (api 20 r/s, export 6 r/m → 429).
- **0.7 `.dockerignore`**; **0.8 backups** (`scripts/backup.sh`/`restore.sh` + docs).
- **P1 scrapers**: **Planet Aid**, **USAgain** (no Ohio coverage yet), **Wearable Collections** (NYC-only), plus the existing Salvation Army + OSM ingest and Goodwill (enrich-only). All wired into `pipeline/seed.py`.
- **P1 region config** (`pipeline/regions.py`) — `columbus` (default), `ohio` (statewide), and **`greater_ohio`** (multi-state: Ohio + bordering MI/IN/KY/WV/PA, with a cross-state ZIP sweep). Selected via `REGION` env.
- **P1 data-quality gate** — `base.py` `_in_us` rejects out-of-US / `0,0` / swapped-lat-lon coords and tallies rejects into `scrape_log.detail`.
- **P1 reconciliation circuit breaker** — closure detection refuses to retire links when a run saw `< RECONCILE_MIN_SEEN` (default 5) records, or would retire `> RECONCILE_MAX_FRACTION` (default 0.40) of a source's in-region links; both env-overridable, region-scoped, skipped on any per-record error.
- **P1 submission content-screen** (reject links/emails/control-chars).
- **P1 prod compose + Caddy TLS**, non-root container, api/web healthchecks, least-privilege DB role (`deploy/app_role.sql`).
- **P1 a11y/UX**: loading/empty/error states, focus management/ESC on the submit dialog, label/ARIA, aria-live toasts, reduced-motion, **keyboard/screen-reader list view + category filter** (`frontend/js/list.js`).
- **`consignment` org_type** (migration 0003) added after `thrift_store`.
- **Community photos + photo voting + photo-validated pin correction** (migration 0004 + `routers/images.py` + `frontend/js/photos.js`): upload with EXIF-strip + Turnstile + per-IP daily cap, helpful/unhelpful votes, auto-applied pin correction once a correction photo reaches score ≥ 3.
- **Turnstile on image votes** (migration 0005 adds `image_votes.turnstile_hash`) — mirrors the location-vote + photo-upload gates.
- **Drag-to-fix pin corrections + community signals + engagement-tiered trust** (migrations 0006/0007 + `routers/corrections.py` + `frontend/js/corrections.js`, `attributes.js`, `pindrag.js`): drag a pin to suggest a position, applied automatically once it clears an **engagement-tiered** support threshold (Cold <3 / Warm 3–14 / Hot 15+ distinct contributions). A correction is **anchored to the pin's immutable `origin_geom`** and capped at **2 km** (API guard + DB trigger) so it can't be walked across town; optional **GPS corroboration** is computed on-device and sent as a **boolean only** (coordinates never stored/correlated/sold; GPS only boosts, never gates a good-faith fix). Retirement now requires deny support to **strictly** outweigh confirmations. Per-attribute value bounds + per-IP daily caps on corrections/ratings. *Accepted tradeoff:* a lone Warm submitter with self-asserted GPS can still auto-apply a fix, deliberately bounded by the 2 km cap + origin anchor + Turnstile.
- **Drop-a-pin submission + reverse geocode** (`/api/reverse`, `submit.js`): add a location by dragging a marker; the street address auto-fills.
- **Light/dark theme system** (`theme.js` + first-paint inline boot, CSP-hash-whitelisted so it can't be XSS-injected) with an auto + manual toggle; Turnstile theme follows the mode.
- **Focus management & a11y hardening** (resolves the P1 *Focus management* item): popover, drag-to-fix sheet, photo gallery/upload modals, and submit panel now capture/restore focus, trap Tab (modals), expose `aria-modal`/`role=dialog`/`aria-pressed`, and the search box is a keyboard-navigable combobox listbox (arrow keys + `aria-activedescendant`). Darkened `--ink-3` to meet **WCAG AA** contrast.
- **New features**: **location search** (`/api/geosearch`), **"use my location"**, **zoom-out cap**, satellite-mode contrast, persisted basemap.
- **National coverage (all 50 states + DC)** — the region layer is now **data-driven** from a vendored ZIP table (`pipeline/data/us_zips.csv`): `state_regions()` synthesizes a region per state (bbox derived from each state's ZIP centroids) plus a `usa` union, selectable via `REGION=<code>` / `REGION=usa` alongside the curated `columbus`/`ohio`/`greater_ohio`. Shipped with: a **gentle, resumable overnight seeder** (`pipeline/seed_national.py` + `scripts/seed_national.sh`) checkpointed per-state in the new `seed_progress` table (**migration 0008**) — interruptible and re-runnable, with a once-at-the-end dedup/promote finalize and a `SEED_FORCE` override; **scraper politeness/backoff** (see below); **OSM national tiling** (see below); and a self-framing frontend — `/api/meta` now returns a `coverage` bbox/center and the map fits to whatever data is actually loaded (Columbus seed → Columbus view, national seed → continent), defaulting to a US view. *Build-only by design: the capability + seeder ship, but no live national seed is run here — it's meant to be kicked off deliberately against the live APIs.*

Remaining open items below are the still-unaddressed P1/P2 entries (DB pool tuning, hours normalization, incremental dedup, CSV export, SRI, i18n, salt rotation, etc.).

## What's already strong (don't regress)
- **Security fundamentals:** all SQL parameterized; XSS-safe output (`esc()`); race-safe vote cooldown (`pg_advisory_xact_lock`); trusted `X-Real-IP` (spoofed XFF can't bypass cooldown, with a test); raw IPs never stored (salted hashes); Turnstile fails closed.
- **Data integrity:** storage-policy enforced end-to-end (Goodwill persists nothing); field-provenance invariant; dedup predicate handles the known traps; ODbL attribution embedded *in* the export payload.
- **Robustness:** schema-aware DB healthcheck + `_wait_for_db` retry; conservative degradation when Nominatim/CF/Overpass are down; clean shared-loader scraper architecture.

---

## P0 — Do first (foundational / high-leverage, mostly low effort)

| # | Item | Why | Impact/Effort | Where |
|---|------|-----|---|---|
| 0.1 | **Add a LICENSE** (code) + data-license note | The repo calls itself "community-owned… civic infrastructure" but has **no license → legally all-rights-reserved**, so nobody can fork/self-host/contribute. Biggest gap vs the mission. (AGPL-3.0 fits "must stay open"; MIT/Apache for max adoption — your call.) Export data is ODbL (OSM is a stored source → share-alike). | H / S | repo root |
| 0.2 | **Fix the broken test command + add CI** | README's `docker compose run api pytest` fails — **pytest isn't in the image** (dev deps only in pyproject). And there's **no CI** at all, so dedup/confidence/export regressions aren't caught. | H / M | `backend/Dockerfile`, `requirements-dev.txt`, `.github/workflows/` |
| 0.3 | **Scheduled re-sync job** | Data **freezes after the one-time seed** — the staleness penalty decays every location toward `pending` while sources never refresh. Directly contradicts the freshness mission. Add a cron/sidecar running `pipeline.sync` on a cadence. | H / M | `docker-compose.yml`, `pipeline/seed.py` |
| 0.4 | **Secrets fail-fast guard** | `IP_HASH_SALT='change-me-in-prod'`, `POSTGRES_PASSWORD=opendrop`, and the **Turnstile TEST secret (accepts every token)** are all silently usable in prod. Refuse to boot (gated on `APP_ENV=prod`) if any default remains. | H / S | `backend/app/config.py` |
| 0.5 | **Read-endpoint caching** (`Cache-Control`) | `/api/locations` re-queries PostGIS on **every pan/zoom** with zero cache reuse — highest-leverage perf win. Add short `Cache-Control` + nginx caching; longer TTL on `/export`. | H / S | `routers/locations.py`, `meta.py`, `nginx.conf` |
| 0.6 | **nginx security headers + gzip** | No CSP / `nosniff` / frame / referrer headers, no compression. CSP is the strongest XSS backstop; gzip cuts repeat-visit bytes. | M / S | `frontend/nginx.conf` |
| 0.7 | **`.dockerignore`** | Build context is the repo root with no ignore → `.git`, `.env` (secrets), `pgdata/`, `research/`, `planning/`, tests all ship into the image. | M / S | repo root |
| 0.8 | **Backups + `down -v` warning** | Only persistence is the `pgdata` volume; no backup job or restore docs, and `docker compose down -v` silently wipes votes + crowd data. | H / S | README, prod compose |

---

## P1 — Next (production-readiness & the national mission)

**Trust & abuse**
- **Moderation gate on crowd submissions** (H/M) — `POST /locations` auto-promotes any geocodable submission to the public map (authority 20 ≥ active floor) with **no content screen**. Start crowd-only at `pending` until a second independent confirm, OR route to `awaiting` for `promote.py` review; add a name/address denylist + URL/email rejection. (`routers/locations.py`)
- **Read rate-limiting + bounded `/export`** (M/M) — all GET endpoints are unthrottled; `/export` dumps the whole dataset with no LIMIT/stream (cheap DoS lever). Add nginx `limit_req` + stream/paginate export. (`nginx.conf`, `meta.py`)
- **DB least-privilege role** (M/M) — the API connects as the schema-owning role (full DDL). Add a restricted app role (no DDL). (`docker-compose.yml`, migration)
- **Moderation/takedown policy + tool** (M/S) — `location_status='hidden'` exists but nothing sets it; no takedown channel for a wrongly-listed address/residence. Add `MODERATION.md` + `python -m pipeline.hide <id>` + a README contact. (civic)

**Data & pipeline (the national thesis)**
- ~~**Deletion / closure detection** (H/L)~~ — **DONE** (see Shipped). Reconciliation in `scrapers/base.py` `_reconcile` retires links not seen in a run (region-scoped), now guarded by the circuit breaker (`RECONCILE_MIN_SEEN` / `RECONCILE_MAX_FRACTION`).
- ~~**Implement the 3 orphan scrapers** (H/L)~~ — **DONE** (see Shipped). `planet_aid`, `usagain`, and `wearable_collections` now have real scrapers wired into `pipeline/seed.py` (USAgain has no Ohio coverage; Wearable Collections is NYC-only).
- ~~**Region abstraction** (M/L)~~ — **DONE** (see Shipped). `pipeline/regions.py` defines `columbus` / `ohio` / `greater_ohio` as data, selected via `REGION` env.
- ~~**Scraper retry/backoff + politeness** (M/M)~~ — **DONE** (see Shipped). `scrapers/http.py` `PoliteClient` adds inter-request pacing, exponential backoff with jitter, and `Retry-After` / 429 / 5xx handling (all env-tunable); covered by `tests/test_http_polite.py`.
- ~~**OSM fixture-fallback hazard** (M/S)~~ — **DONE** (see Shipped). The specific national hazard is closed: `osm_ingest.fetch` now substitutes the committed Columbus fixture **only when the region bbox actually covers Columbus** (`_covers_fixture`), so a national/other-state run can never seed Columbus bins elsewhere — it logs and yields nothing for that region instead. (*Still open as polish: an explicit `OSM_ALLOW_FIXTURE` off-switch and recording fixture use in `scrape_log`.*) Covered by `tests/test_osm_tiling.py`.
- ~~**Data-quality gate** (M/S)~~ — **DONE** (see Shipped). `base.py` `_in_us` rejects out-of-US / `0,0` / swapped-lat-lon coords and counts rejects in `scrape_log.detail`.

**Ops & deploy**
- **Prod compose + TLS** (H/M) — only a dev compose exists (plain HTTP, raw ports). Add `docker-compose.prod.yml` + a TLS reverse proxy (Caddy = auto-LetsEncrypt + HSTS in ~3 lines). README claims prod is "configured" — it isn't yet. (`docker-compose.yml`, README)
- **api/web healthchecks + non-root container** (M/S) — only `db` has a healthcheck; `web` depends on `api` by start-order only; uvicorn runs as **root**. Add healthchecks, `condition: service_healthy`, and a `USER app`. (`docker-compose.yml`, `Dockerfile`)
- **Structured logging + request IDs + metrics** (M/M) — only `basicConfig(INFO)`; swallowed geocode/Turnstile failures log nothing. Add request-id middleware, JSON logs, `/metrics`, log rotation, resource limits. (`main.py`, compose)
- **Wire migrations into deploy** (M/M) — `migrate.sh` ledger exists but nothing runs it for existing DBs; a future `0002` has no automated apply path. Add a one-shot `migrate` service or api entrypoint. (compose)
- **DB pool sizing/timeouts from env + `db_unavailable` 503 guard** (M/S) — pool is hardcoded `max_size=10`, no `statement_timeout`; routers assume `db.pool` non-None (cold start → opaque 500). (`db.py`, routers)

**Accessibility & UX (core function is non-visual-inaccessible)**
- ~~**Keyboard/SR access to the map** (H/L)~~ — **DONE** (see Shipped). `frontend/js/list.js` adds a togglable, focusable keyboard/screen-reader **list view** plus a category filter.
- ~~**Focus management** (H/M)~~ — **DONE** (see Shipped). Popover, drag-to-fix sheet, photo modals, and submit panel capture/restore focus, trap Tab (modals), and toggle `aria-hidden`/`aria-modal` correctly; ESC closes; search is a combobox listbox. (`submit.js`, `popover.js`, `corrections.js`, `photos.js`, `search.js`)
- **Loading / empty / error states** (H/M) — a zero-result viewport or a down API shows a **silent blank map**. Add spinner + "no locations here" + "couldn't reach server" states. (`main.js`, `markers.js`)
- **"Locate me" + click-to-place + geocode preview** (M/L) — mobile users at a bin must type a full address and never see where it landed; map always opens at hardcoded Columbus. (`map.js`, `submit.js`)
- **Real `<form>` semantics** (M/M) — submit is loose inputs on a div: no Enter-to-submit, no native validation, State accepts any 2 chars. (`submit.js`)
- **Satellite-mode chrome contrast + persist basemap** (M/M) — white cards over dark imagery need stronger borders; remember the chosen layer. (`map.js`, `style.css`)

---

## P2 — Polish & longer-term

- **Hours/address normalization** (M/M) — nothing populates the structured `hours` JSONB (only `hours_raw`); add an `opening_hours` parser + `{"always":true}` for 24/7 bins; split Planet Aid's combined address so dedup tier-2 can fire. (`store.py`, scrapers)
- **Incremental dedup** (M/M) — full O(pairs) sweep every run; dedup only touched rows, reserve full sweep for maintenance; iterate to fixed point on 3-way clusters. (`dedup.py`)
- **CSV/bulk export + data manifest + UI download** (M/M) — export is GeoJSON-only, undiscoverable, in-memory; add `?format=csv`, `Content-Disposition`, a dataset manifest (license/attribution/cadence), and a "Download data" link. (`meta.py`, frontend)
- **OpenAPI response models + examples** (L/M) — handlers return bare dicts; document the points-vs-clusters union, error envelope, etc. for downstream consumers. (routers)
- **Geocode caching + Nominatim policy** (M/M) — cache normalized-address→coords; document self-host/alt-provider; respect 1 req/s; consider moving geocode to the async `promote` path so submit never blocks on an external call. (`geocode.py`)
- **SRI on CDN assets** (M/S) — add `integrity=` hashes (or vendor Leaflet/markercluster locally to drop the runtime CDN dependency and tighten CSP). (`index.html`)
- **In-flight fetch cancellation + small bbox cache** (L/M) — `moveend` refetches with no `AbortController` (stale-response race) and no cache on pan-back. (`main.js`)
- **IP-hash salt rotation + honest privacy claim** (M/M) — fixed salt = stable lifelong correlation + offline-reversible if it leaks; consider monthly `HMAC(master, yyyymm)` windowed salt; align DATA_MODEL's privacy claim with reality. (`security.py`)
- **i18n seam** (L/M) — extract UI strings (relevant for large Spanish-speaking communities). (frontend)
- **Source-payload history** (L/M) — upsert overwrites raw payload, losing refresh-diffs; log significant geometry drift. (`store.py`)
- **a11y quick wins** — `aria-live` on toasts, `<noscript>`, label `for/id`, emoji `aria-hidden` + visually-hidden labels, `prefers-reduced-motion`, ZIP `inputmode`, autocomplete, SEO/OG meta. (frontend)

---

## Suggested next sprint (highest value / lowest risk first)
1. **0.1 LICENSE**, **0.4 secrets guard**, **0.7 `.dockerignore`**, **0.6 nginx headers+gzip**, **0.8 backups doc** — all small, all close real gaps.
2. **0.2 pytest-in-image + GitHub Actions CI** — makes the existing tests a real gate.
3. **0.5 read caching** + **P1 read rate-limit** — perf + abuse-cost bound together.
4. ~~**0.3 scheduled re-sync** + **P1 deletion detection**~~ — **DONE** (deletion detection now has a reconciliation circuit breaker).
5. ~~**P1 Planet Aid scraper**~~ — **DONE** (Planet Aid + USAgain + Wearable Collections all shipped).
6. **P1 moderation gate** + ~~a11y list-view~~/focus/empty-states — list-view shipped; moderation gate + focus management remain for trust + inclusivity before any public launch.

*(This pass implemented several P0/P2 quick wins — see the commit following this file.)*
