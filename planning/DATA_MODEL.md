# OpenDrop — Data Model

> Full PostGIS schema in SQL DDL. This is the canonical contract: every table, column, type, index, constraint, function, and trigger Phase 3 implements. The migration file [`migrations/0001_init.sql`](../migrations/) is the verbatim source of truth; this document is its annotated form. Target: **PostgreSQL 17 + PostGIS 3.5** — the official non-alpine `postgis/postgis:17` image currently ships PostGIS **3.5**; PostGIS 3.6 is current upstream but its non-alpine PG17 image is not yet published, and OpenDrop's schema uses **no 3.6-only feature** (all spatial functions used exist since PostGIS ≤3.0). See [FINDINGS.md](../research/FINDINGS.md) Finding 5 / decision D3.

> **Migration mechanism:** `0001_init.sql` contains plain (non-idempotent) `CREATE` statements — `CREATE TYPE … AS ENUM` has no `IF NOT EXISTS` form, so the file is **applied exactly once**, tracked by a ledger. `scripts/migrate.sh` first ensures `CREATE TABLE IF NOT EXISTS schema_migrations(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())`, then applies each `NNNN_*.sql` whose `version` is absent and records it — so re-running is a no-op without requiring self-idempotent DDL. On first container boot the Postgres `initdb` mount applies `0001_init.sql` directly; `migrate.sh` is for existing databases.

## Design principles (traceable to Phase 1)

1. **Every stored row is policy-tagged.** `location_sources` joins `sources`, which carries `storage_policy` (`ingest` vs `enrich_only`) and `license`. Only `ingest`-policy data is persisted to canonical/redistributable rows. Implements decisions **D1** (Goodwill enrich-only) and **D2** (clothedonations not stored). (FINDINGS → Redistributability Matrix.)
2. **Confidence never depends on OSM hours/`collection_times`** (0% coverage). It is a function of *source authority + agreement*, *crowd votes*, and *staleness*. (FINDINGS open item 1.)
3. **Dedup is first-class.** Canonical `locations` carry a normalized-name expression index (pg_trgm) and a GIST geometry index so the validated predicate `brand-equal AND ≤300 m AND name-sim ≥0.4` runs efficiently. (FINDINGS Finding 4.)
4. **Privacy:** raw IPs are never stored — only salted SHA-256 hashes. No auth system (per directive); abuse gating is IP-hash cooldown + Turnstile.

---

## 0. Extensions & enums

```sql
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- fuzzy name similarity for dedup
CREATE EXTENSION IF NOT EXISTS citext;       -- case-insensitive source refs

-- Kind of donation location.
CREATE TYPE org_type AS ENUM (
  'charity_store',     -- shop=charity (Goodwill/SA storefronts)
  'thrift_store',      -- shop=second_hand
  'drop_bin',          -- unattended clothing/textile bin
  'donation_center',   -- staffed drop-off (GreenDrop-style)
  'mutual_aid',        -- community closet / free store
  'church_drive',      -- congregation collection
  'other'
);

-- Lifecycle of a canonical location.
CREATE TYPE location_status AS ENUM (
  'active',            -- confidence >= DISPLAY_FLOOR; shown on map
  'pending',           -- confidence < DISPLAY_FLOOR; hidden, recoverable via votes/sources
  'merged',            -- deduped into another row (see merged_into_id)
  'hidden'             -- manually suppressed (spam/abuse); terminal
);

CREATE TYPE vote_kind AS ENUM ('confirm', 'deny');

CREATE TYPE storage_policy AS ENUM ('ingest', 'enrich_only');

CREATE TYPE pending_status AS ENUM ('awaiting', 'promoted', 'rejected', 'duplicate');

CREATE TYPE scrape_status AS ENUM ('success', 'partial', 'failed');
```

---

## 1. `sources` — data-source catalog (governs storage policy & attribution)

One row per data source. Drives the redistributability rules and the confidence authority weights. Not in the directive's minimum list, but it is the mechanism that implements the directive's redistribution principle ("OSM is ODbL — attribute it; Google/Foursquare enrich-only").

```sql
CREATE TABLE sources (
  code             text PRIMARY KEY,              -- 'osm','salvation_army',...
  display_name     text        NOT NULL,
  storage_policy   storage_policy NOT NULL,       -- ingest => persisted/redistributable
  authority_weight smallint    NOT NULL DEFAULT 0 -- contribution to confidence (0..50)
                     CHECK (authority_weight BETWEEN 0 AND 50),
  license          text        NOT NULL,          -- e.g. 'ODbL-1.0','first-party-attribution'
  attribution      text        NOT NULL,          -- shown in UI attribution control
  homepage         text,
  created_at       timestamptz NOT NULL DEFAULT now()
);

INSERT INTO sources (code, display_name, storage_policy, authority_weight, license, attribution, homepage) VALUES
 ('osm',                 'OpenStreetMap',        'ingest',      40, 'ODbL-1.0',                 '© OpenStreetMap contributors (ODbL)',        'https://www.openstreetmap.org'),
 ('salvation_army',      'The Salvation Army',   'ingest',      50, 'first-party-attribution',  'Data: The Salvation Army (satruck.org)',     'https://satruck.org'),
 ('planet_aid',          'Planet Aid',           'ingest',      50, 'first-party-attribution',  'Data: Planet Aid',                           'https://www.planetaid.org'),
 ('usagain',             'USAgain',              'ingest',      45, 'first-party-attribution',  'Data: USAgain',                              'https://usagain.com'),
 ('wearable_collections','Wearable Collections', 'ingest',      45, 'first-party-attribution',  'Data: Wearable Collections',                 'https://www.wearablecollections.com'),
 ('crowd',               'Community submissions','ingest',      20, 'CC0-crowd',                'Community-contributed',                       NULL),
 ('goodwill',            'Goodwill',             'enrich_only', 50, 'proprietary-tos',          'Goodwill (verified at query time)',          'https://www.goodwill.org');
-- goodwill is enrich_only (D1): scrapers run it but the loader never persists canonical rows from it.
```

---

## 2. `locations` — canonical location record

```sql
CREATE TABLE locations (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  geom             geometry(Point, 4326) NOT NULL,
  name             text        NOT NULL,
  org_type         org_type    NOT NULL DEFAULT 'other',
  org_name         text,                              -- brand/operator: 'Goodwill','The Salvation Army'
  brand_key        text,                              -- canonicalized brand token for dedup (see §7.5/ARCH §7.4); NULL = unbranded

  -- Structured address (any may be NULL; OSM gives ~36% coverage)
  address_line     text,
  house_number     text,                              -- leading house-number token parsed from address (dedup tier-2)
  city             text,
  state            varchar(2),                        -- USPS 2-letter (varchar, not char — avoids blank-pad/regex footgun)
  postal_code      text,

  -- Hours: normalized structure + original string (OSM collection_times is 0%, so mostly NULL)
  hours            jsonb,                             -- {"mon":[["09:00","17:00"]], ...} or {"always":true}
  hours_raw        text,
  accepted_items   text[],
  phone            text,
  website          text,

  -- Confidence & lifecycle (see §6 for the formula)
  confidence       numeric(5,2) NOT NULL DEFAULT 0,
  status           location_status NOT NULL DEFAULT 'pending',
  merged_into_id   bigint REFERENCES locations(id) ON DELETE SET NULL,

  -- Denormalized counters (kept in sync by triggers; speed up confidence + display)
  upvotes          integer     NOT NULL DEFAULT 0,
  denies           integer     NOT NULL DEFAULT 0,
  source_count     integer     NOT NULL DEFAULT 0,
  is_redistributable boolean   NOT NULL DEFAULT true, -- false if only enrich_only sources (never exported)

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  last_verified_at timestamptz,                       -- max(source last_seen, last confirm vote)

  CONSTRAINT confidence_range CHECK (confidence >= 0 AND confidence <= 100),
  CONSTRAINT merged_has_target CHECK (status <> 'merged' OR merged_into_id IS NOT NULL),
  CONSTRAINT state_format CHECK (state IS NULL OR state ~ '^[A-Z]{2}$')
);

-- Immutable name normalizer (mirrors pipeline/dedup name rules; used for trigram index & matching)
CREATE OR REPLACE FUNCTION normalize_name(txt text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT trim(regexp_replace(
           regexp_replace(lower(coalesce(txt, '')), '[^a-z0-9 ]', ' ', 'g'),
           '\s+', ' ', 'g'));
$$;

-- Indexes
CREATE INDEX locations_geom_gix      ON locations USING gist (geom);
CREATE INDEX locations_status_ix     ON locations (status);
CREATE INDEX locations_state_ix      ON locations (state);
CREATE INDEX locations_org_type_ix   ON locations (org_type);
CREATE INDEX locations_name_trgm_ix  ON locations USING gin (normalize_name(name) gin_trgm_ops);
CREATE INDEX locations_active_geom_gix ON locations USING gist (geom) WHERE status = 'active';
CREATE INDEX locations_brand_key_ix  ON locations (brand_key) WHERE brand_key IS NOT NULL;
```

**Field-provenance invariant (redistribution safety — defense in depth for D1/D2).**
Canonical display columns (`name, org_name, brand_key, address_*, house_number, city, state, postal_code, hours, hours_raw, accepted_items, phone, website`) may be written **only** from an `ingest`-policy `location_sources` record, choosing the value from the contributing source with the highest `authority_weight` (then most-recent `last_seen_at`). The loader/merge logic must **never** write a canonical column from an `enrich_only` record. This closes the *field-level* leak that the row-level `is_redistributable` flag cannot catch (a row with both ingest and enrich sources is `is_redistributable=true`, so only this invariant prevents an enrich value from riding out via `v_public_locations`). A Phase-3 test asserts it.

**Notes**
- `geom` is SRID 4326 (WGS84). Distance checks use `geography` casts (`ST_DWithin(geom::geography, …, 300)`) so thresholds are in **meters** without a projected SRID. (Matches the dedup spec's metric thresholds.)
- `hours` JSONB schema: `{ "mon": [["09:00","17:00"]], "tue": [...], ..., "always": false }`. `{"always": true}` denotes 24/7 bins (USAgain). Absent days = closed/unknown.
- `is_redistributable=false` rows are excluded from the public data export endpoint and any bulk dump (D1/D2 enforcement at the row level).

---

## 3. `location_sources` — provenance (one row per contributing source)

```sql
CREATE TABLE location_sources (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id   bigint NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  source_code   text   NOT NULL REFERENCES sources(code),
  source_ref    citext NOT NULL,                 -- external id: 'node/1234', LocationGUID, planetaid id...
  source_geom   geometry(Point, 4326),           -- the source's own coordinate (for re-dedup/merge)
  source_name   text,                            -- name as that source reports it
  payload       jsonb,                           -- raw normalized record (provenance / refresh diff)
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT uq_source_ref UNIQUE (source_code, source_ref)  -- one external record -> one canonical link
);

CREATE INDEX location_sources_location_ix ON location_sources (location_id);
CREATE INDEX location_sources_code_ix     ON location_sources (source_code);
```

**Invariant:** the loader only ever inserts rows here for `ingest`-policy sources. `enrich_only` sources (Goodwill) are fetched, normalized, and dedup-matched in the pipeline but **never** persisted here — they appear only in `scrape_log`. This is the concrete enforcement of D1.

---

## 4. `votes` — crowd validation (append-only, per location, per IP-hash)

```sql
CREATE TABLE votes (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id    bigint    NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  vote           vote_kind NOT NULL,
  ip_hash        text      NOT NULL,            -- sha256(IP_HASH_SALT || client_ip); raw IP never stored
  turnstile_hash text,                          -- sha256 of the (single-use) Turnstile token, for audit
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Cooldown lookup: "any vote from this ip_hash on this location in the last 24h?"
CREATE INDEX votes_cooldown_ix ON votes (location_id, ip_hash, created_at DESC);
CREATE INDEX votes_location_ix ON votes (location_id);
```

The 24-hour cooldown (directive requirement) is enforced in a single transaction by the API:
`SELECT 1 FROM votes WHERE location_id=$1 AND ip_hash=$2 AND created_at > now() - interval '24 hours'`.
Append-only design preserves vote history for future abuse analysis without blocking legitimate re-confirmation after the window.

---

## 5. `pending_locations` — crowd-submitted intake queue

New community submissions land here first (never directly into `locations`). The **promotion step** (lifecycle §8 here; mechanism in [ARCHITECTURE §7.6](ARCHITECTURE.md)) geocodes, dedup-checks, and either promotes them into `locations` (creating a `crowd` `location_sources` row, `status='promoted'`, `promoted_location_id` set) or marks them `duplicate`/`rejected`.

```sql
CREATE TABLE pending_locations (
  id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name                text     NOT NULL,
  org_type            org_type NOT NULL DEFAULT 'other',
  address_line        text,
  city                text,
  state               varchar(2),
  postal_code         text,
  geom                geometry(Point, 4326),       -- geocoded on submit; NULL => needs review
  submitter_ip_hash   text     NOT NULL,
  turnstile_hash      text,
  status              pending_status NOT NULL DEFAULT 'awaiting',
  dupe_candidate_id   bigint REFERENCES locations(id) ON DELETE SET NULL,
  promoted_location_id bigint REFERENCES locations(id) ON DELETE SET NULL,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT state_format_pending CHECK (state IS NULL OR state ~ '^[A-Z]{2}$')
);

CREATE INDEX pending_status_ix ON pending_locations (status);
CREATE INDEX pending_geom_gix  ON pending_locations USING gist (geom);
```

---

## 6. `scrape_log` — per-source run history (freshness tracking)

```sql
CREATE TABLE scrape_log (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_code      text NOT NULL REFERENCES sources(code),
  run_started_at   timestamptz NOT NULL DEFAULT now(),
  run_finished_at  timestamptz,
  status           scrape_status,
  records_fetched  integer NOT NULL DEFAULT 0,
  records_upserted integer NOT NULL DEFAULT 0,
  records_new      integer NOT NULL DEFAULT 0,
  records_merged   integer NOT NULL DEFAULT 0,
  error            text,
  detail           jsonb     -- e.g. {"zips_swept":1043,"bbox":[...],"enrich_matches":12}
);

CREATE INDEX scrape_log_source_ix ON scrape_log (source_code, run_started_at DESC);
```

For an `enrich_only` source, `records_fetched`/`enrich_matches` are logged here while `records_upserted=0` — this is the audit trail proving the Goodwill scraper ran without persisting (D1).

---

## 7. Confidence formula & recompute (trigger-driven)

> "Confidence score recalculation triggered on every vote write." — directive. Implemented as a SQL function called by triggers on `votes` and by the pipeline loader after source changes.

```
confidence = clamp(0, 100,  source_component + crowd_component − staleness_penalty )

source_component  = min(85, Σ authority_weight of DISTINCT ingest-policy contributing sources)
crowd_component   = clamp(−40, +30,  5·upvotes − 8·denies)   -- denies weigh more (enable takedown)
staleness_penalty = min(20, 2 · months_since(last_verified_at))   -- 0 if never verified-stale

DISPLAY_FLOOR = 25   -- status 'active' iff confidence >= 25, else 'pending'
UI buckets: high >=70, medium 40..69, low 25..39
```

```sql
CREATE OR REPLACE FUNCTION recompute_confidence(p_location_id bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_source numeric;
  v_crowd  numeric;
  v_stale  numeric;
  v_conf   numeric;
  v_up     integer;
  v_dn     integer;
  v_redist boolean;
  v_lastv  timestamptz;
BEGIN
  -- Source component: sum authority of distinct INGEST sources (enrich_only excluded entirely).
  -- v_redist = "has >= 1 ingest source" (the join already filters to ingest, so COUNT(*)>0 says it).
  SELECT COALESCE(LEAST(85, SUM(s.authority_weight)), 0),
         COUNT(*) > 0
    INTO v_source, v_redist
  FROM (SELECT DISTINCT ls.source_code FROM location_sources ls WHERE ls.location_id = p_location_id) d
  JOIN sources s ON s.code = d.source_code AND s.storage_policy = 'ingest';

  v_source := COALESCE(v_source, 0);
  v_redist := COALESCE(v_redist, false);

  SELECT upvotes, denies, last_verified_at INTO v_up, v_dn, v_lastv
  FROM locations WHERE id = p_location_id;

  v_crowd := GREATEST(-40, LEAST(30, 5 * v_up - 8 * v_dn));
  v_stale := CASE
               WHEN v_lastv IS NULL THEN 0
               ELSE LEAST(20, 2 * (EXTRACT(EPOCH FROM (now() - v_lastv)) / 2592000.0))
             END;

  v_conf := GREATEST(0, LEAST(100, v_source + v_crowd - v_stale));

  -- Community deny-dominance override: a clear deny majority retires ANY location,
  -- regardless of source authority. Without this, a deduped multi-source row
  -- (source_component up to 85) could never fall below DISPLAY_FLOOR since crowd
  -- is floored at -40 (85-40=45). Threshold needs distinct ip-hashes (cooldown-gated),
  -- so it resists casual single-actor abuse while still letting the crowd take down dead spots.
  IF v_dn >= 5 AND v_dn >= v_up + 5 THEN
    v_conf := LEAST(v_conf, 20);   -- force below DISPLAY_FLOOR; keeps score coherent with 'pending'
  END IF;

  UPDATE locations
     SET confidence = round(v_conf, 2),
         is_redistributable = v_redist,
         status = CASE
                    WHEN status IN ('merged','hidden') THEN status   -- terminal/manual unchanged
                    WHEN v_conf >= 25 THEN 'active'
                    ELSE 'pending'
                  END,
         updated_at = now()
   WHERE id = p_location_id;
END; $$;

-- Keep denormalized vote counters in sync, then recompute, on every vote write.
CREATE OR REPLACE FUNCTION trg_after_vote() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_id bigint := COALESCE(NEW.location_id, OLD.location_id);
BEGIN
  UPDATE locations l SET
     upvotes = (SELECT count(*) FROM votes v WHERE v.location_id = v_id AND v.vote='confirm'),
     denies  = (SELECT count(*) FROM votes v WHERE v.location_id = v_id AND v.vote='deny'),
     last_verified_at = CASE WHEN TG_OP='INSERT' AND NEW.vote='confirm'
                             THEN GREATEST(COALESCE(l.last_verified_at, now()), now())
                             ELSE l.last_verified_at END
   WHERE l.id = v_id;
  PERFORM recompute_confidence(v_id);
  RETURN NULL;
END; $$;

CREATE TRIGGER votes_after_write
  AFTER INSERT OR DELETE ON votes
  FOR EACH ROW EXECUTE FUNCTION trg_after_vote();

-- Maintain source_count + recompute when provenance changes (pipeline ingest/dedup).
CREATE OR REPLACE FUNCTION trg_after_source() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_id bigint := COALESCE(NEW.location_id, OLD.location_id);
BEGIN
  UPDATE locations l SET
     source_count = (SELECT count(DISTINCT source_code) FROM location_sources WHERE location_id = v_id),
     -- GREATEST() skips NULLs, so when v_id has no remaining sources this keeps the prior
     -- last_verified_at (never invents a 1970/epoch timestamp -> never a phantom -20 staleness).
     last_verified_at = GREATEST(l.last_verified_at,
                                 (SELECT max(last_seen_at) FROM location_sources WHERE location_id = v_id))
   WHERE l.id = v_id;
  PERFORM recompute_confidence(v_id);
  RETURN NULL;
END; $$;

CREATE TRIGGER location_sources_after_write
  AFTER INSERT OR UPDATE OR DELETE ON location_sources
  FOR EACH ROW EXECUTE FUNCTION trg_after_source();
```

**Worked examples (Phase 4 manual-test compatibility).** All absolute numbers below assume `last_verified_at = now()` (a freshly ingested/verified row, `staleness = 0`); staleness reduces the absolute confidence over time but does **not** change any of the status outcomes below.

- *Single-source.* An active Salvation-Army-only location: source 50, 0 votes, fresh → confidence **50** → `active` (medium bucket).
  - Deny it **4×** (4 distinct IP-hashes — 4 is the true minimum): crowd = `max(−40, −8·4) = −32` → confidence `50 − 32 = 18` → `< 25` → status flips to **`pending`** (hidden). ✓ Directive Phase 4 step 2.
- *Multi-source (what the seed actually produces after dedup).* A deduped OSM(40)+Salvation Army(50) location: source = `min(85, 90) = 85` → confidence **85** → `active` (high bucket). Denies alone cap crowd at −40 (85−40 = 45, still active) — so the **deny-dominance override** is what retires it: **5 denies** with 0 confirms satisfies `denies≥5 AND denies≥confirms+5` → confidence floored to ≤20 → **`pending`**. ✓ This is the location the Phase-4 deny test should target; the seed log identifies a multi-source row for the tester.
- *Upvote → score updates.* A confirm vote sets `last_verified_at = now()` (zeroing staleness) **and** adds `+5` crowd, so confidence visibly **rises** even against a previously-stale row — the freshness refresh guarantees the directive's "score updates" assertion can't be masked by accumulated staleness. ✓ Directive Phase 4 step 2.

---

## 8. Location lifecycle (state machine)

```
[submit] -> pending_locations(awaiting)
                 |  promotion job: geocoded? not a dupe?
                 |--- dupe -------> pending_locations(duplicate)  (attach dupe_candidate_id)
                 |--- no geom ----> pending_locations(awaiting)   (await manual geocode/review)
                 +--- ok ---------> locations(status=pending, +crowd source, conf~20)
                                          |  votes / additional sources raise confidence
                                          +--- conf>=25 --> locations(active)   [shown]
                                          +--- conf<25  --> locations(pending)  [hidden]
 dedup job: two locations match predicate -> keep canonical, other -> status=merged (merged_into_id)
 abuse:    manual -> status=hidden (terminal)
```

`pending_locations` is the **intake/vetting** queue; `locations.status='pending'` is the **low-confidence** state of an already-canonical row. Both are legitimately "pending" but distinct stages — kept separate by design.

---

## 9. Public export view (redistributable subset)

```sql
CREATE VIEW v_public_locations AS
  SELECT id, geom, name, org_type, org_name, address_line, city, state, postal_code,
         hours, accepted_items, phone, website, confidence, status, last_verified_at
  FROM locations
  WHERE status = 'active' AND is_redistributable = true;
```

This view is the only thing the bulk-export / open-data endpoint reads — guaranteeing no `enrich_only`-tainted row ever leaves the system (D1/D2).
