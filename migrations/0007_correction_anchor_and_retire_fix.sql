-- OpenDrop — correction anchor, strict retire dominance, tighter attribute bounds (migration 0007)
-- Source of truth: planning/DATA_MODEL.md (engagement-tiered trust model).
--
-- Follow-up to 0006. Four targeted fixes surfaced by an adversarial review of the trust model.
-- All are append-only redefinitions / additive schema — nothing in 0006 is edited in place
-- (the migration chain is an ordered ledger; we fix shipped migrations with a NEW one).
--
--  (a) PIN-WALK FIX — anchor the 2 km correction cap to an IMMUTABLE origin, not the live geom.
--      In 0006 the distance cap was measured against the CURRENT geom, which a correction then
--      overwrites. A patient attacker could therefore "walk" a pin across the map in <2 km hops,
--      each hop legal relative to the previous one. We add locations.origin_geom (set once at
--      insert, never moved by a correction) and measure every correction against it instead.
--
--  (b) RETIRE DOMINANCE — require denies to STRICTLY exceed confirms before capping confidence.
--      0006 capped when `v_dn >= v_floor AND v_dn >= v_up`. The `>=` meant a perfectly balanced
--      community (equal confirms and denies) retired a location on a bare tie. A tie is not a
--      consensus to remove — it's a dispute. We change the second clause to `v_dn > v_up`.
--
--  (c) ATTRIBUTE BOUNDS — replace the blanket 1..50 CHECK with a per-attribute range, matching
--      the API (safety/condition are 1..3 scales; only bins is a 1..50 count). The DB is the
--      source of truth, so it should reject an out-of-range safety=42 even if the API is bypassed.
--
--  (d) OBSERVABLE BACKFILL — the re-evaluation loop (needed because (b) changes the retire rule)
--      now RAISEs NOTICE with the number of rows whose status flipped, instead of mutating
--      silently.

BEGIN;

-- (a) Immutable correction anchor ------------------------------------------------------------
-- origin_geom records where a location FIRST landed (seed import or crowd submission). A
-- correction may move geom; origin_geom never moves. Backfill existing rows to their current
-- position (the best origin we can reconstruct) and keep it filled on every future insert via a
-- BEFORE INSERT trigger, so all insert paths (pipeline seed, crowd submit, drop-a-pin) are covered.
ALTER TABLE locations ADD COLUMN IF NOT EXISTS origin_geom geometry(Point, 4326);
UPDATE locations SET origin_geom = geom WHERE origin_geom IS NULL;

CREATE OR REPLACE FUNCTION trg_set_origin_geom() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.origin_geom IS NULL THEN
    NEW.origin_geom := NEW.geom;
  END IF;
  RETURN NEW;
END; $$;

DROP TRIGGER IF EXISTS locations_set_origin ON locations;
CREATE TRIGGER locations_set_origin
  BEFORE INSERT ON locations
  FOR EACH ROW EXECUTE FUNCTION trg_set_origin_geom();

-- Redefine recompute_correction: identical to 0006 EXCEPT the distance cap is anchored to the
-- immutable origin (COALESCE for any row created before this migration's backfill ran). The
-- submitter GPS self-weight is deliberately left UNCHANGED — a lone on-site submitter can still
-- apply a Warm fix alone, bounded now by the origin anchor + the 2 km cap + Turnstile.
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

  -- Auto-apply once support reaches the tier threshold AND the move is within the accuracy cap
  -- measured from the IMMUTABLE origin (not the current geom) so corrections cannot walk a pin.
  IF v_support >= v_req THEN
    SELECT ST_DWithin(COALESCE(l.origin_geom, l.geom)::geography,
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

-- (b) Strict deny dominance ------------------------------------------------------------------
-- Identical to the 0006 body EXCEPT the override now requires v_dn > v_up (a tie is a dispute,
-- not a consensus to retire).
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

  -- Engagement-tiered deny-dominance: denies must reach the tier floor AND STRICTLY exceed the
  -- confirms before they cap confidence (retire). A bare tie is a dispute, not a removal signal.
  v_eng   := location_engagement(p_location_id);
  v_floor := retire_deny_floor(v_eng);
  IF v_dn >= v_floor AND v_dn > v_up THEN
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

-- (c) Per-attribute value bounds -------------------------------------------------------------
-- safety/condition are 1..3 ordinal scales; bins is a 1..50 count. The blanket 1..50 from 0006
-- let a safety=42 through at the DB layer. Existing rows already satisfy the tighter rule (the
-- API has always enforced it), so the re-validating ADD is safe.
ALTER TABLE attribute_votes DROP CONSTRAINT IF EXISTS attribute_value_range;
ALTER TABLE attribute_votes ADD CONSTRAINT attribute_value_range CHECK (
  (attribute IN ('safety', 'condition') AND value BETWEEN 1 AND 3)
  OR (attribute = 'bins' AND value BETWEEN 1 AND 50)
);

-- Support the per-IP-per-day attribute rate-limit query added to the API in this release.
CREATE INDEX IF NOT EXISTS attribute_votes_ip_ix ON attribute_votes (ip_hash, updated_at);

-- (d) Observable re-evaluation ---------------------------------------------------------------
-- The retire rule changed in (b), so re-run confidence on every live row and report how many
-- flipped status (instead of mutating silently like 0006's backfill).
DO $$
DECLARE
  r        record;
  v_before text;
  v_after  text;
  v_flips  integer := 0;
BEGIN
  FOR r IN SELECT id, status FROM locations WHERE status NOT IN ('merged','hidden') LOOP
    v_before := r.status;
    PERFORM recompute_confidence(r.id);
    SELECT status INTO v_after FROM locations WHERE id = r.id;
    IF v_after IS DISTINCT FROM v_before THEN
      v_flips := v_flips + 1;
    END IF;
  END LOOP;
  RAISE NOTICE 'migration 0007: re-evaluation flipped status on % location(s)', v_flips;
END $$;

INSERT INTO schema_migrations (version) VALUES ('0007_correction_anchor_and_retire_fix.sql') ON CONFLICT DO NOTHING;

COMMIT;
