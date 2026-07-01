-- OpenDrop — photo-optional pin corrections + community signals (migration 0006)
-- Source of truth: planning/DATA_MODEL.md (engagement-tiered trust model).
--
-- WHY THIS EXISTS
-- Until now the only way to fix a pin was to upload a PHOTO and have it earn helpful votes
-- (0004). That is heavy: a desktop user who just notices a seed pin is 150 ft off has no
-- recourse. This migration adds a lightweight, photo-OPTIONAL "correction" (drag a pin to where
-- the bin actually is) plus community signals (perceived safety, bin condition, # of bins).
--
-- THE TRUST MODEL — trust scales INVERSELY with how invested the community already is.
--   * Engagement E = count of DISTINCT participants (by ip_hash) across every interaction.
--   * Tiers:  Cold  E<3   ·  Warm  3..14  ·  Hot  E>=15
--   * A correction auto-applies once its WEIGHTED support reaches the tier threshold:
--       Cold 1 (good faith) · Warm 2 (or 1 + GPS) · Hot 4 (GPS counts double).
--   * GPS "I'm standing here" only ADDS weight. We store a BOOLEAN, never coordinates — the
--     client computes the distance and sends true/false. Never stored, correlated, or sold.
--   * Closure asymmetry: the deny-dominance retire rule is generalized to the SAME tiers, so a
--     busy (Hot) location needs a much stronger deny signal than a fresh one. A handful of
--     stray "it's gone" reports must not retire a heavily-used Salvation Army.
--
-- All new write paths are Turnstile-gated at the API layer, exactly like votes/photos.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE correction_status AS ENUM ('open', 'applied', 'rejected', 'superseded');

-- 1. Pin-move proposals (photo optional) ------------------------------------
CREATE TABLE location_corrections (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id       bigint NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  suggested_lat     double precision NOT NULL,
  suggested_lon     double precision NOT NULL,
  note              text,
  image_id          bigint REFERENCES location_images(id) ON DELETE SET NULL, -- optional supporting photo
  submitter_ip_hash text    NOT NULL,
  turnstile_hash    text,
  gps_corroborated  boolean NOT NULL DEFAULT false, -- submitter was within ~75m (client-computed; coords never stored)
  confirmations     integer NOT NULL DEFAULT 0,     -- weighted support from OTHER voters (excludes submitter)
  rejections        integer NOT NULL DEFAULT 0,     -- count of "no" votes
  support           integer NOT NULL DEFAULT 0,     -- submitter weight + confirmer weights (drives auto-apply)
  required_support  integer NOT NULL DEFAULT 1,     -- snapshot of the tier threshold at last recompute
  status            correction_status NOT NULL DEFAULT 'open',
  applied           boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  applied_at        timestamptz,
  CONSTRAINT correction_lat_range CHECK (suggested_lat BETWEEN -90  AND 90),
  CONSTRAINT correction_lon_range CHECK (suggested_lon BETWEEN -180 AND 180)
);
CREATE INDEX location_corrections_loc_ix    ON location_corrections (location_id, status);
CREATE INDEX location_corrections_open_ix   ON location_corrections (location_id) WHERE status = 'open';

-- 2. Confirm / reject votes on a specific proposal --------------------------
CREATE TABLE correction_votes (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  correction_id    bigint  NOT NULL REFERENCES location_corrections(id) ON DELETE CASCADE,
  ip_hash          text    NOT NULL,
  confirm          boolean NOT NULL,                 -- true = "yes, move it here"; false = "no"
  gps_corroborated boolean NOT NULL DEFAULT false,   -- voter was standing at the suggested point
  turnstile_hash   text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_correction_vote UNIQUE (correction_id, ip_hash)
);

-- 3. Community attribute ratings (safety / condition / bins) ----------------
-- These never move pins or change confidence; they surface in the popover AND they feed the
-- engagement measure that decides how hard a location is to move or retire. One row per
-- (location, ip, attribute); re-rating updates in place.
CREATE TABLE attribute_votes (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id    bigint   NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  ip_hash        text     NOT NULL,
  attribute      text     NOT NULL,
  value          smallint NOT NULL,
  turnstile_hash text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT attribute_name CHECK (attribute IN ('safety', 'condition', 'bins')),
  -- safety/condition are 1..3 scales (poor/ok/good); bins is a small count estimate (1..50).
  CONSTRAINT attribute_value_range CHECK (value BETWEEN 1 AND 50),
  CONSTRAINT uq_attribute_vote UNIQUE (location_id, ip_hash, attribute)
);
CREATE INDEX attribute_votes_loc_ix ON attribute_votes (location_id, attribute);

-- 4. Engagement + tier ------------------------------------------------------
-- Engagement = number of DISTINCT participants (by ip_hash) who have touched a location in ANY
-- way. Used only to SCALE thresholds. An attacker can raise E (by brigading), but that only
-- makes a location HARDER to vandalize, not easier — denies, photos, and votes all count, so a
-- vandal inflating engagement raises their own bar. E can never be lowered by an attacker.
CREATE OR REPLACE FUNCTION location_engagement(p_location_id bigint)
RETURNS integer LANGUAGE sql STABLE AS $$
  SELECT count(DISTINCT h)::int FROM (
    SELECT ip_hash AS h        FROM votes               WHERE location_id = p_location_id
    UNION ALL
    SELECT submitter_ip_hash   FROM location_images     WHERE location_id = p_location_id
    UNION ALL
    SELECT iv.ip_hash          FROM image_votes iv
                               JOIN location_images li ON li.id = iv.image_id
                               WHERE li.location_id = p_location_id
    UNION ALL
    SELECT submitter_ip_hash   FROM location_corrections WHERE location_id = p_location_id
    UNION ALL
    SELECT cv.ip_hash          FROM correction_votes cv
                               JOIN location_corrections lc ON lc.id = cv.correction_id
                               WHERE lc.location_id = p_location_id
    UNION ALL
    SELECT ip_hash             FROM attribute_votes     WHERE location_id = p_location_id
  ) s WHERE h IS NOT NULL;
$$;

-- Weighted support a correction needs to auto-apply, by engagement tier.
--   Cold E<3 -> 1 (good faith) · Warm 3..14 -> 2 · Hot E>=15 -> 4 (GPS confirmer counts double).
CREATE OR REPLACE FUNCTION correction_required_support(p_engagement integer)
RETURNS integer LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_engagement < 3 THEN 1
              WHEN p_engagement < 15 THEN 2
              ELSE 4 END;
$$;

-- Deny count needed to force the retire (confidence-cap) override, by engagement tier.
--   Cold -> 2 · Warm -> 4 · Hot -> 8.  (A fresh spot retires easily; a busy one needs a crowd.)
CREATE OR REPLACE FUNCTION retire_deny_floor(p_engagement integer)
RETURNS integer LANGUAGE sql IMMUTABLE AS $$
  SELECT CASE WHEN p_engagement < 3 THEN 2
              WHEN p_engagement < 15 THEN 4
              ELSE 8 END;
$$;

-- 5. Correction consensus + auto-apply --------------------------------------
-- A correction is an ACCURACY fix ("the bin is 150 ft north, inside the lot"), not a way to
-- relocate a business across town. Beyond CORRECTION_MAX_MOVE_M (2 km — kept in sync with the
-- API guard in backend/app/routers/corrections.py) the move will not auto-apply.
CREATE OR REPLACE FUNCTION recompute_correction(p_correction_id bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_loc     bigint;
  v_lat     double precision;
  v_lon     double precision;
  v_sub_gps boolean;
  v_applied boolean;
  v_status  correction_status;
  v_eng     integer;
  v_req     integer;
  v_conf_w  integer;   -- weighted confirmations from OTHER voters (1 each, 2 if GPS)
  v_reject  integer;   -- count of "no" votes
  v_support integer;   -- submitter weight + confirmer weights
  v_within  boolean;
BEGIN
  SELECT location_id, suggested_lat, suggested_lon, gps_corroborated, applied, status
    INTO v_loc, v_lat, v_lon, v_sub_gps, v_applied, v_status
  FROM location_corrections WHERE id = p_correction_id;

  IF v_loc IS NULL THEN RETURN; END IF;

  SELECT COALESCE(SUM(CASE WHEN confirm THEN 1 + (gps_corroborated)::int ELSE 0 END), 0),
         COALESCE(SUM(CASE WHEN NOT confirm THEN 1 ELSE 0 END), 0)
    INTO v_conf_w, v_reject
  FROM correction_votes WHERE correction_id = p_correction_id;

  -- Submitter contributes weight 1 (2 if they were standing at the spot).
  v_support := (1 + (v_sub_gps)::int) + v_conf_w;

  v_eng := location_engagement(v_loc);
  v_req := correction_required_support(v_eng);

  UPDATE location_corrections
     SET confirmations = v_conf_w, rejections = v_reject,
         support = v_support, required_support = v_req
   WHERE id = p_correction_id;

  -- Already resolved? recompute is idempotent — stop here.
  IF v_applied OR v_status <> 'open' THEN RETURN; END IF;

  -- Reject when clearly out-voted (>=2 rejects and rejects lead the weighted confirms).
  IF v_reject >= 2 AND v_reject > v_conf_w THEN
    UPDATE location_corrections SET status = 'rejected' WHERE id = p_correction_id;
    RETURN;
  END IF;

  -- Auto-apply once support reaches the tier threshold AND the move is within the accuracy cap.
  IF v_support >= v_req THEN
    SELECT ST_DWithin(l.geom::geography,
                      ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326)::geography, 2000)
      INTO v_within FROM locations l WHERE l.id = v_loc;

    IF COALESCE(v_within, false) THEN
      UPDATE locations
         SET geom = ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326), updated_at = now()
       WHERE id = v_loc;
      UPDATE location_corrections
         SET status = 'applied', applied = true, applied_at = now()
       WHERE id = p_correction_id;
      -- Any other open proposals for this location are now moot.
      UPDATE location_corrections
         SET status = 'superseded'
       WHERE location_id = v_loc AND id <> p_correction_id AND status = 'open';
    END IF;
  END IF;
END; $$;

-- After-insert on a correction: a Cold (good-faith) fix applies immediately here.
CREATE OR REPLACE FUNCTION trg_after_correction() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM recompute_correction(NEW.id);
  RETURN NULL;
END; $$;

CREATE TRIGGER location_corrections_after_insert
  AFTER INSERT ON location_corrections
  FOR EACH ROW EXECUTE FUNCTION trg_after_correction();

CREATE OR REPLACE FUNCTION trg_after_correction_vote() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_id bigint := COALESCE(NEW.correction_id, OLD.correction_id);
BEGIN
  PERFORM recompute_correction(v_id);
  RETURN NULL;
END; $$;

CREATE TRIGGER correction_votes_after_write
  AFTER INSERT OR UPDATE OR DELETE ON correction_votes
  FOR EACH ROW EXECUTE FUNCTION trg_after_correction_vote();

-- 6. Generalize the deny-dominance retire rule to the engagement tiers ------
-- Reproduces the current recompute_confidence body verbatim EXCEPT the override block, which
-- becomes engagement-tiered (see retire_deny_floor). Existing behavior is preserved for the
-- cases the test-suite pins: a fresh/low-engagement row still retires on a handful of denies;
-- a heavily-engaged row now demands a much larger deny crowd.
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
  v_eng    integer;
  v_floor  integer;
BEGIN
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

  -- Engagement-tiered deny-dominance: denies must reach the tier floor AND at least match the
  -- confirms before they cap confidence (retire). Busy locations (Hot) need 8 denies; fresh
  -- ones (Cold) only 2. Replaces the old flat "v_dn>=5 AND v_dn>=v_up+5".
  v_eng   := location_engagement(p_location_id);
  v_floor := retire_deny_floor(v_eng);
  IF v_dn >= v_floor AND v_dn >= v_up THEN
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

-- Re-evaluate every live row so the new tiered retire rule takes effect immediately
-- (mirrors the recompute loop in 0002).
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT id FROM locations WHERE status NOT IN ('merged','hidden') LOOP
    PERFORM recompute_confidence(r.id);
  END LOOP;
END $$;

INSERT INTO schema_migrations (version) VALUES ('0006_corrections_and_signals.sql') ON CONFLICT DO NOTHING;

COMMIT;
