# OpenDrop Operations Runbook

The single source of truth for running OpenDrop in production: deploying changes, recovering from
failure, moderating content, and responding to incidents. Pair it with `planning/ARCHITECTURE.md`
(how the system is built) and `.env.example` (every tunable).

---

## 1. System at a glance

| Service | Image | Role | Host port |
|---------|-------|------|-----------|
| `db`  | `postgis/postgis:17-3.5` | PostgreSQL + PostGIS — all map state | — (internal) |
| `api` | built from `backend/Dockerfile` | FastAPI read/write API | `${API_PORT}` → 8000 |
| `web` | `nginx:alpine` | serves `frontend/` + reverse-proxies `/api`, `/media` | `${WEB_PORT}` → 80 |
| `scheduler` | `opendrop-api` (profile `scheduler`) | opt-in nightly re-sync | — |

**Two stores must both be backed up to have a restore point:**
- the **`pgdata`** volume (the database — locations, votes, corrections, audit), and
- the **`media`** volume (uploaded, EXIF-stripped photos referenced by the DB).

**Editable live vs. baked into the image:**
- `frontend/` is a read-only bind mount → **HTML/CSS/JS edits are live on the next request** (no rebuild).
- `backend/` (Python) is **baked into the `api` image** → API changes require a rebuild + recreate.

---

## 2. Production environment prerequisites

Before any production boot, `.env` must set real secrets. The API **refuses to start** in
`APP_ENV=prod` if any of these still hold a dev default (`Settings.assert_production_secrets`):

- `IP_HASH_SALT` — must be a long random string (not empty / `change-me-in-prod`).
- `TURNSTILE_SECRET` — must be a real Cloudflare secret (not a `1x…/2x…/3x…` test key).
- `DATABASE_URL` — must not use the default `:opendrop@` password.

Also set for production:
- `APP_ENV=prod` — enables the secrets guard **and** the schema-at-head boot check, and disables
  `/docs`, `/redoc`, `/openapi.json`.
- `OPERATOR_TOKEN` — a long random secret. Leave **empty** to keep the entire `/api/admin/*`
  surface returning 404 (moderation disabled, invisible to probes).
- `POSTGRES_PASSWORD` / `DATABASE_URL` — matching strong DB credentials.
- `DOMAIN` / `ACME_EMAIL` — for the Caddy TLS front in `docker-compose.prod.yml`.

> Generate a secret: `openssl rand -hex 32`.

---

## 3. Deploy / cutover procedure

> **GATE:** Applying migrations to the live database and rebuilding the live `api` image is a
> production-mutating action. Do not run §3.2–§3.3 against live without an explicit operator
> go-ahead. §3.1 (backup) is always safe and always required first.

### 3.0 Pre-deploy checklist
- [ ] CI green on the commit being shipped (pytest + ruff + migrate-twice idempotency).
- [ ] `EXPECTED_SCHEMA_VERSION` in the image matches the newest migration filename.
- [ ] A fresh backup exists (§3.1) and the **restore drill** has passed at least once (§4.3).
- [ ] `.env` reviewed against §2 (real secrets, `APP_ENV=prod`, `OPERATOR_TOKEN` decision made).
- [ ] Maintenance expectation communicated if a long migration is involved.

### 3.1 Back up first (always)
```bash
bash scripts/backup.sh                      # writes ./backups/opendrop-{db,media}-<TS>.* + .sha256
```

### 3.2 Apply migrations (gated)
Migrations are idempotent and self-recording; re-running is a no-op.
```bash
# From the repo root, against the live DB (inside the compose network):
docker compose exec -T db sh -lc 'psql "$DATABASE_URL" -c "SELECT version FROM schema_migrations ORDER BY version"'
DATABASE_URL=postgresql://opendrop:<pw>@localhost:5432/opendrop bash scripts/migrate.sh
```
`migrate.sh` applies only the missing `migrations/*.sql` in order and records each in
`schema_migrations`. Migration **0010 is additive and carries no backfill** — it only changes the
behaviour of *future* corrections, so it is safe to apply under live traffic.

### 3.3 Rebuild + recreate the API (gated)
```bash
docker compose build api
docker compose up -d api          # recreate only api; db + web keep serving
docker compose logs -f api        # watch for the boot line; see §3.4
```
On boot in prod the API asserts the schema is at `EXPECTED_SCHEMA_VERSION` and **refuses to start**
if the row is missing — so a "new code vs. old schema" mismatch fails fast and loudly instead of
erroring at request time. (This is the failure mode recorded in memory as *live-deploy-drift*:
running code far ahead of the DB. The boot assertion is the guardrail against repeating it.)

### 3.4 Post-deploy verification
```bash
curl -fsS http://localhost:${WEB_PORT:-8080}/api/health           # {"status":"ok",...}
curl -fsS "http://localhost:${WEB_PORT:-8080}/api/locations?bbox=-83.25,39.80,-82.75,40.18" | head -c 200
# operator surface reachable only with the token (expect 200 with, 404 without):
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:${WEB_PORT:-8080}/api/admin/reports
curl -s -o /dev/null -w '%{http_code}\n' -H "X-Operator-Token: $OPERATOR_TOKEN" http://localhost:${WEB_PORT:-8080}/api/admin/reports
```

### 3.5 Rollback
- **Code only (schema unchanged):** redeploy the previous image tag —
  `docker compose build api && docker compose up -d api` from the prior commit. Fast, safe.
- **Schema involved:** migrations have no automatic `down`. To roll back a bad migration, restore
  the §3.1 backup into a scratch DB, confirm, then promote (§4.2 with `--force`). Because 0010 is
  additive with no backfill, reverting *it* specifically means dropping its new objects — prefer
  forward-fixing over down-migrating unless data is corrupted.
- **Bad bulk data change (not schema):** use the moderation revert tools (§5.4) — `revert-actor`
  and `revert-all` unwind auto-applied corrections without a full restore.

---

## 4. Backup & disaster recovery

### 4.1 What/when
`scripts/backup.sh` captures **both** the DB (custom-format `pg_dump -Fc`) and the **media** volume,
checksums them, and prunes to the newest `BACKUP_RETENTION` (default 14) sets.

**Recommended schedule (cron on the host):**
```cron
# nightly at 03:17 UTC, keep 30 days, log output
17 3 * * *  cd /opt/opendrop && BACKUP_RETENTION=30 bash scripts/backup.sh /opt/opendrop/backups >> /var/log/opendrop-backup.log 2>&1
```
Copy `./backups` off-box (object storage / second host) — a backup on the same disk as `pgdata`
does not survive a disk loss.

### 4.2 Restore
```bash
# Rehearse into a scratch DB (never touches live):
bash scripts/restore.sh backups/opendrop-db-<TS>.dump --db opendrop_drill

# Real recovery into live (guarded; needs --force to overwrite a populated DB), with photos:
bash scripts/restore.sh backups/opendrop-db-<TS>.dump backups/opendrop-media-<TS>.tgz --force
```
`restore.sh` verifies the set's `.sha256` before touching anything and **refuses to overwrite a
database that already has locations** unless `--force` is given.

### 4.3 RPO / RTO
- **RPO (max data loss):** = backup interval. With nightly backups, up to ~24h of community
  contributions. Tighten by running `backup.sh` more often (it is incremental-free but cheap — the
  DB dump is ~100 KB–low MB at current scale) or by enabling WAL archiving for point-in-time
  recovery if sub-day RPO is required.
- **RTO (time to restore):** minutes. Round-trip drill at current scale completes in well under a
  minute (DB restore + media untar). Budget RTO = pull backup from off-box + `restore.sh` +
  `docker compose up -d` + §3.4 verification.
- **Drill cadence:** run §4.2 into `opendrop_drill` monthly and before every gated cutover. A backup
  you have never restored is a hypothesis, not a backup.

---

## 5. Moderation operator guide

All `/api/admin/*` routes require the `X-Operator-Token` header and return **404** (not 401/403)
when the token is unset or wrong — the surface is invisible without the secret. Public reporting
needs no token.

### 5.1 Public reporting (no auth)
- `POST /api/locations/{id}/report` — files a complaint. **Never auto-hides a location.**
- `POST /api/images/{id}/report` — once `REPORT_IMAGE_HIDE_THRESHOLD` (default 2) *distinct*
  reporters flag a photo, it is soft-hidden (`removed_at` set, file kept — reversible). A lone
  report only files a complaint.

Body: `{ "reason": "<=500 chars, screened", "turnstile_token": "..." }`. Rate-limited to
`REPORTS_PER_IP_PER_DAY` across both endpoints.

### 5.2 Review queue
```bash
curl -s -H "X-Operator-Token: $OPERATOR_TOKEN" http://HOST/api/admin/reports | jq
```
- `POST /api/admin/reports/{id}/resolve` `{ "note": "..." }` — mark a report handled.

### 5.3 Takedown / restore
- `POST /api/admin/locations/{id}/takedown` `{ "reason": "..." }` → status `hidden`, 404 to public,
  open reports auto-resolved. `…/restore` returns it to `active` (confidence ≥ 25) or `pending`.
- `POST /api/admin/images/{id}/takedown` `{ "reason": "..." }` → sets `removed_at` **and unlinks the
  file from disk**. `…/restore` clears `removed_at` and reports whether the file is still present
  (a taken-down file is gone; restore brings back the row, not the bytes).

### 5.4 Audit trail & revert
Every auto-applied field/pin correction writes a `moderation_audit` row.
- `GET  /api/admin/locations/{id}/audit` — the change history for one location.
- `POST /api/admin/audit/{id}/revert` `{ "note": "..." }` — undo one change. Idempotent: a second
  revert returns 409. A revert that finds the value already overwritten by a newer legitimate edit
  is recorded as a no-op ("superseded") rather than clobbering it.
- `POST /api/admin/locations/{id}/revert-all` `{ "note": "..." }` — unwind a location newest→oldest
  back to origin.
- `POST /api/admin/revert-actor` `{ "actor_ip_hash": "...", "note": "..." }` — undo **every**
  auto-applied change from one actor across all locations (mass-edit cleanup). Get the
  `actor_ip_hash` from the audit rows.

### 5.5 The authoritative-source threshold gate (0010)
A lone good-faith edit to `name`, `org_name`, or `address` on an **authoritatively-sourced**
location (any non-`crowd` source — e.g. a Salvation Army seed row) no longer auto-applies; it needs
≥1 confirmer even when engagement is Cold. `org_type`, pin moves, and crowd-only pins keep the
normal engagement-tiered behaviour. This makes single-actor identity rewrites of seed data require
corroboration. The gate affects only **future** corrections.

---

## 6. Monitoring & uptime

- **Liveness:** `GET /api/health` → `{"status":"ok"}`. Point an external uptime monitor
  (UptimeRobot, Healthchecks.io, a cron `curl`) at `https://<domain>/api/health` on a 1–5 min
  interval; alert on non-200 or timeout. This is the canary for "API up + DB reachable".
- **Request tracing:** every response carries `X-Request-ID`; the API logs a structured access line
  per request (method, path, status, ms, request id) and wraps unhandled errors in a 500 envelope
  carrying the same id — quote it when investigating.
- **Disk:** the media volume is capped at `MEDIA_MAX_TOTAL_BYTES` (uploads past it return 507).
  Still alert on host disk < 15% free — `pgdata` is uncapped and grows with the dataset.
- **Logs:** `docker compose logs -f api` (access + errors), `… logs -f db`, `… logs -f web`.

---

## 7. Incident response

**API won't boot after deploy.** Check `docker compose logs api`. Likely the prod schema/secret
guard: either a migration is missing (run §3.2) or `.env` still has a dev default (§2). The guard is
intentional — fix the cause, don't disable it.

**Database down / unreachable.** `docker compose ps`; `… logs db`. If the volume is intact,
`docker compose up -d db` and wait for the schema-aware healthcheck. If `pgdata` is lost, this is a
DR event → §4.2 restore.

**Disk full.** If media: confirm `MEDIA_MAX_TOTAL_BYTES` is enforced and prune taken-down files. If
`pgdata`: free space, then `VACUUM`. Never `docker volume rm` a live volume.

**Abuse flood (spam submissions / vote stuffing / mass edits).**
1. Identify the actor from `moderation_audit.actor_ip_hash` or report patterns.
2. `revert-actor` to undo their auto-applied changes (§5.4).
3. Tighten the relevant per-IP caps in `.env` (`SUBMIT_PER_IP_PER_DAY`, `CORRECTIONS_PER_IP_PER_DAY`,
   `IMAGE_UPLOADS_PER_IP_PER_DAY`, `REPORTS_PER_IP_PER_DAY`) and recreate `api`.
4. Extend `CONTENT_DENYLIST` for recurring spam phrases.

**Bad bulk data change.** Prefer the revert tools (§5.4) over a full restore — they are surgical and
keep unrelated community contributions since the backup. Full restore (§4.2) only if the corruption
is widespread or structural.

---

## 8. Routine maintenance

- **Nightly re-sync (optional):** `docker compose --profile scheduler up -d` runs `pipeline.sync`
  every `SYNC_INTERVAL_SECONDS`. The reconciliation circuit breaker (`RECONCILE_MIN_SEEN`,
  `RECONCILE_MAX_FRACTION`) prevents a truncated upstream response from mass-retiring a region.
- **Salt rotation (privacy lever):** rotating `IP_HASH_SALT` permanently severs every stored hash
  from any future IP (documented in `frontend/privacy.html`). It also resets per-IP cooldowns and
  rate-limit counters, so rotate deliberately.
- **Backup retention:** tune `BACKUP_RETENTION`; confirm off-box copies are landing.
