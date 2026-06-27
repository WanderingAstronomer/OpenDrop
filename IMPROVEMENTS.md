# OpenDrop — Improvement Roadmap

> Output of a 7-dimension project-wide scan (frontend/UX/a11y, backend API, pipeline/data, security/abuse, ops/deploy, tests/CI, data/civic-licensing). The system is **working and validated** (Phases 1–4); this is the path from "validated demo" to "production civic infrastructure." Items are tagged **impact** (H/M/L) and **effort** (S/M/L), grouped by priority. Cross-cutting themes flagged by multiple reviewers are called out.

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
- **Deletion / closure detection** (H/L) — loader only inserts/upserts; a closed store/bin never disappears (the exact staleness failure OpenDrop exists to beat). Reconcile per source+region: links not seen in N runs → remove (trigger already drops confidence). (`scrapers/base.py`, `store.py`)
- **Implement the 3 orphan scrapers** (H/L) — `planet_aid`, `usagain`, `wearable_collections` are pre-registered in the `sources` catalog (overstating coverage) but have **no scraper**; FINDINGS already specifies their endpoints/dedup keys. Planet Aid (~10k bins) is the cleanest and biggest bin-coverage win. (`pipeline/scrapers/`)
- **Region abstraction** (M/L) — coverage is hardcoded to Columbus (ZIP list / seed point / bbox). Move to a regions config / ZIP-centroid generator so a new metro is *data, not code*. (`scrapers/*`, `config.py`)
- **Scraper retry/backoff + politeness** (M/M) — back-to-back ZIP sweeps with no delay/retry; a transient failure is a silent coverage hole. Shared httpx helper with backoff + inter-request sleep + `Retry-After`. (`scrapers/*`)
- **OSM fixture-fallback hazard** (M/S) — any Overpass failure silently substitutes the committed Columbus fixture and logs success — fine for the demo, a correctness hazard in a scheduled national run. Gate behind `OSM_ALLOW_FIXTURE`; record fixture use in `scrape_log`. (`osm_ingest.py`)
- **Data-quality gate** (M/S) — validate coords fall in a US bbox (catch 0,0 / swapped lat-lon), reject empty names; count rejects in `scrape_log.detail`. (`scrapers/base.py`)

**Ops & deploy**
- **Prod compose + TLS** (H/M) — only a dev compose exists (plain HTTP, raw ports). Add `docker-compose.prod.yml` + a TLS reverse proxy (Caddy = auto-LetsEncrypt + HSTS in ~3 lines). README claims prod is "configured" — it isn't yet. (`docker-compose.yml`, README)
- **api/web healthchecks + non-root container** (M/S) — only `db` has a healthcheck; `web` depends on `api` by start-order only; uvicorn runs as **root**. Add healthchecks, `condition: service_healthy`, and a `USER app`. (`docker-compose.yml`, `Dockerfile`)
- **Structured logging + request IDs + metrics** (M/M) — only `basicConfig(INFO)`; swallowed geocode/Turnstile failures log nothing. Add request-id middleware, JSON logs, `/metrics`, log rotation, resource limits. (`main.py`, compose)
- **Wire migrations into deploy** (M/M) — `migrate.sh` ledger exists but nothing runs it for existing DBs; a future `0002` has no automated apply path. Add a one-shot `migrate` service or api entrypoint. (compose)
- **DB pool sizing/timeouts from env + `db_unavailable` 503 guard** (M/S) — pool is hardcoded `max_size=10`, no `statement_timeout`; routers assume `db.pool` non-None (cold start → opaque 500). (`db.py`, routers)

**Accessibility & UX (core function is non-visual-inaccessible)**
- **Keyboard/SR access to the map** (H/L) — markers are mouse-click-only; a screen-reader user gets an empty region. Add a togglable, focusable **list view** of in-viewport locations synced to `render()`. (`markers.js`)
- **Focus management** (H/M) — submit panel & popover never receive focus, no ESC-to-close, no focus trap, and the panel stays `aria-hidden=true` even when open. (`submit.js`, `popover.js`)
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
4. **0.3 scheduled re-sync** + **P1 deletion detection** — make freshness real (the mission).
5. **P1 Planet Aid scraper** — proves the multi-source/national path with the biggest bin win.
6. **P1 moderation gate** + **a11y list-view/focus/empty-states** — trust + inclusivity before any public launch.

*(This pass implemented several P0/P2 quick wins — see the commit following this file.)*
