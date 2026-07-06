-- OpenDrop — initial schema (migration 0001)
-- Source of truth: docs/DATA_MODEL.md (Phase 2, post-review).
-- Target: PostgreSQL 17 + PostGIS 3.5 (no 3.6-only feature used).
-- Applied ONCE: plain CREATEs (CREATE TYPE has no IF NOT EXISTS). The ledger in
-- scripts/migrate.sh guards re-application; the docker initdb mount runs this on first boot.

BEGIN;

-- Migration ledger (self-recorded at end). Lets scripts/migrate.sh skip already-applied
-- files even though this one was applied via the docker initdb mount.
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

-- 0. Extensions -------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS citext;

-- Enums
CREATE TYPE org_type AS ENUM (
  'charity_store', 'thrift_store', 'consignment', 'drop_bin', 'donation_center',
  'mutual_aid', 'church_drive', 'other'
);
CREATE TYPE location_status AS ENUM ('active', 'pending', 'merged', 'hidden');
CREATE TYPE vote_kind AS ENUM ('confirm', 'deny');
CREATE TYPE storage_policy AS ENUM ('ingest', 'enrich_only');
CREATE TYPE pending_status AS ENUM ('awaiting', 'promoted', 'rejected', 'duplicate');
CREATE TYPE scrape_status AS ENUM ('success', 'partial', 'failed');

-- 1. sources catalog --------------------------------------------------------
CREATE TABLE sources (
  code             text PRIMARY KEY,
  display_name     text           NOT NULL,
  storage_policy   storage_policy NOT NULL,
  authority_weight smallint       NOT NULL DEFAULT 0
                     CHECK (authority_weight BETWEEN 0 AND 50),
  license          text           NOT NULL,
  attribution      text           NOT NULL,
  homepage         text,
  created_at       timestamptz    NOT NULL DEFAULT now()
);

INSERT INTO sources (code, display_name, storage_policy, authority_weight, license, attribution, homepage) VALUES
 ('osm',                 'OpenStreetMap',        'ingest',      40, 'ODbL-1.0',                '© OpenStreetMap contributors (ODbL)',   'https://www.openstreetmap.org'),
 ('salvation_army',      'The Salvation Army',   'ingest',      50, 'first-party-attribution', 'Data: The Salvation Army (satruck.org)', 'https://satruck.org'),
 ('planet_aid',          'Planet Aid',           'ingest',      50, 'first-party-attribution', 'Data: Planet Aid',                      'https://www.planetaid.org'),
 ('usagain',             'USAgain',              'ingest',      45, 'first-party-attribution', 'Data: USAgain',                         'https://usagain.com'),
 ('wearable_collections','Wearable Collections', 'ingest',      45, 'first-party-attribution', 'Data: Wearable Collections',            'https://www.wearablecollections.com'),
 ('crowd',               'Community submissions','ingest',      20, 'CC0-crowd',               'Community-contributed',                 NULL),
 ('goodwill',            'Goodwill',             'enrich_only', 50, 'proprietary-tos',         'Goodwill (verified at query time)',     'https://www.goodwill.org');

-- 2. locations (canonical) --------------------------------------------------
CREATE TABLE locations (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  geom             geometry(Point, 4326) NOT NULL,
  name             text        NOT NULL,
  org_type         org_type    NOT NULL DEFAULT 'other',
  org_name         text,
  brand_key        text,                              -- canonicalized brand token; NULL = unbranded
  address_line     text,
  house_number     text,                              -- leading house-number token (dedup tier-2)
  city             text,
  state            varchar(2),
  postal_code      text,
  hours            jsonb,
  hours_raw        text,
  accepted_items   text[],
  phone            text,
  website          text,
  confidence       numeric(5,2) NOT NULL DEFAULT 0,
  status           location_status NOT NULL DEFAULT 'pending',
  merged_into_id   bigint REFERENCES locations(id) ON DELETE SET NULL,
  upvotes          integer     NOT NULL DEFAULT 0,
  denies           integer     NOT NULL DEFAULT 0,
  source_count     integer     NOT NULL DEFAULT 0,
  is_redistributable boolean   NOT NULL DEFAULT true,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  last_verified_at timestamptz,
  CONSTRAINT confidence_range CHECK (confidence >= 0 AND confidence <= 100),
  CONSTRAINT merged_has_target CHECK (status <> 'merged' OR merged_into_id IS NOT NULL),
  CONSTRAINT state_format CHECK (state IS NULL OR state ~ '^[A-Z]{2}$')
);

CREATE OR REPLACE FUNCTION normalize_name(txt text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT trim(regexp_replace(
           regexp_replace(lower(coalesce(txt, '')), '[^a-z0-9 ]', ' ', 'g'),
           '\s+', ' ', 'g'));
$$;

CREATE OR REPLACE FUNCTION normalize_house_number(txt text) RETURNS text
  LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT (regexp_match(coalesce(txt, ''), '^\s*([0-9]+)'))[1];
$$;

CREATE INDEX locations_geom_gix       ON locations USING gist (geom);
CREATE INDEX locations_status_ix      ON locations (status);
CREATE INDEX locations_state_ix       ON locations (state);
CREATE INDEX locations_org_type_ix    ON locations (org_type);
CREATE INDEX locations_name_trgm_ix   ON locations USING gin (normalize_name(name) gin_trgm_ops);
CREATE INDEX locations_active_geom_gix ON locations USING gist (geom) WHERE status = 'active';
CREATE INDEX locations_brand_key_ix   ON locations (brand_key) WHERE brand_key IS NOT NULL;

-- 3. location_sources (provenance) ------------------------------------------
CREATE TABLE location_sources (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id   bigint NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  source_code   text   NOT NULL REFERENCES sources(code),
  source_ref    citext NOT NULL,
  source_geom   geometry(Point, 4326),
  source_name   text,
  payload       jsonb,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_source_ref UNIQUE (source_code, source_ref)
);

CREATE INDEX location_sources_location_ix ON location_sources (location_id);
CREATE INDEX location_sources_code_ix     ON location_sources (source_code);

-- 4. votes (append-only) ----------------------------------------------------
CREATE TABLE votes (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id    bigint    NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  vote           vote_kind NOT NULL,
  ip_hash        text      NOT NULL,
  turnstile_hash text,
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX votes_cooldown_ix ON votes (location_id, ip_hash, created_at DESC);
CREATE INDEX votes_location_ix ON votes (location_id);

-- 5. pending_locations (crowd intake) ---------------------------------------
CREATE TABLE pending_locations (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name                 text     NOT NULL,
  org_type             org_type NOT NULL DEFAULT 'other',
  address_line         text,
  city                 text,
  state                varchar(2),
  postal_code          text,
  geom                 geometry(Point, 4326),
  submitter_ip_hash    text     NOT NULL,
  turnstile_hash       text,
  status               pending_status NOT NULL DEFAULT 'awaiting',
  dupe_candidate_id    bigint REFERENCES locations(id) ON DELETE SET NULL,
  promoted_location_id bigint REFERENCES locations(id) ON DELETE SET NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT state_format_pending CHECK (state IS NULL OR state ~ '^[A-Z]{2}$')
);

CREATE INDEX pending_status_ix ON pending_locations (status);
CREATE INDEX pending_geom_gix  ON pending_locations USING gist (geom);

-- 6. scrape_log -------------------------------------------------------------
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
  detail           jsonb
);

CREATE INDEX scrape_log_source_ix ON scrape_log (source_code, run_started_at DESC);

-- 7. confidence recompute + triggers ----------------------------------------
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
  -- Source component: sum authority of distinct INGEST sources (enrich_only excluded).
  -- v_redist = has >= 1 ingest source (join already filters to ingest).
  -- NOTE: COALESCE is INSIDE LEAST — LEAST/GREATEST ignore NULLs, so LEAST(85, NULL)
  -- would wrongly return 85 for a source-less location. COALESCE(SUM,0) first -> 0.
  SELECT LEAST(85, COALESCE(SUM(s.authority_weight), 0)),
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

  -- Deny-dominance override: a clear deny majority retires ANY location regardless of
  -- source authority (so deduped multi-source rows are not un-retireable).
  IF v_dn >= 5 AND v_dn >= v_up + 5 THEN
    v_conf := LEAST(v_conf, 20);
  END IF;

  UPDATE locations
     SET confidence = round(v_conf, 2),
         is_redistributable = v_redist,
         status = CASE
                    WHEN status IN ('merged','hidden') THEN status
                    WHEN v_conf >= 25 THEN 'active'
                    ELSE 'pending'
                  END,
         updated_at = now()
   WHERE id = p_location_id;
END; $$;

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

CREATE OR REPLACE FUNCTION trg_after_source() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_id bigint := COALESCE(NEW.location_id, OLD.location_id);
BEGIN
  UPDATE locations l SET
     source_count = (SELECT count(DISTINCT source_code) FROM location_sources WHERE location_id = v_id),
     -- GREATEST skips NULLs: keeps prior value when no sources remain (never a 1970 epoch).
     last_verified_at = GREATEST(l.last_verified_at,
                                 (SELECT max(last_seen_at) FROM location_sources WHERE location_id = v_id))
   WHERE l.id = v_id;
  PERFORM recompute_confidence(v_id);
  RETURN NULL;
END; $$;

CREATE TRIGGER location_sources_after_write
  AFTER INSERT OR UPDATE OR DELETE ON location_sources
  FOR EACH ROW EXECUTE FUNCTION trg_after_source();

-- 8. public export view (redistributable subset) ----------------------------
CREATE VIEW v_public_locations AS
  SELECT id, geom, name, org_type, org_name, address_line, city, state, postal_code,
         hours, accepted_items, phone, website, confidence, status, last_verified_at
  FROM locations
  WHERE status = 'active' AND is_redistributable = true;

-- Self-record in the ledger (idempotent).
INSERT INTO schema_migrations (version) VALUES ('0001_init.sql') ON CONFLICT DO NOTHING;

COMMIT;
