-- OpenDrop — two-band photo pin-move safeguard (migration 0013)
--
-- WHY THIS EXISTS
-- 0012 brought the photo pin-correction path to parity with the drag-pin path (engagement tier,
-- origin-anchored 2 km cap, moderation_audit trail). It still AUTO-APPLIES any move that clears the
-- score gate and sits within 2 km of the immutable origin. Owner review (2026-07-05) decided that a
-- move can be large-but-legitimate OR large-and-abusive, and the score gate alone shouldn't silently
-- teleport a pin a kilometre-plus. This migration splits the post-score-gate decision into TWO BANDS:
--
--   BAND A (auto-apply): move <= 250 m from COALESCE(origin_geom, geom). Apply immediately exactly as
--     0012 (move geom + INSERT moderation_audit kind='pin_correction' applied=true) and mark
--     apply_state='approved'. Small nudges keep the fast path.
--
--   BAND B (hold for operator): move > 250 m and <= 2000 m, AND at least 4 DISTINCT INDEPENDENT
--     helpful upvoters (distinct ip_hash, helpful, ip_hash <> the photo's submitter). The pin is NOT
--     moved; apply_state='pending_review' queues it for a human. An operator later runs
--     apply_pending_image_move() to commit the move (re-checking the 2 km cap) or reject-move to drop it.
--
--   move > 250 m and <= 2000 m with < 4 independent upvoters: apply_state stays 'none'. The photo
--     still vouches per its score; the pin is untouched (no move, no queue).
--
--   move > 2000 m: never applied or queued (unchanged hard ceiling; re-checked at operator apply time).
--
-- The 250 m / 4-voter bands are ADDITIONAL gates layered AFTER the 0012 score gate
-- (v_threshold = GREATEST(3, correction_required_support(location_engagement(v_loc)))). Everything
-- 0012 computed for score/status/upvotes/downvotes and that score gate is preserved byte-for-byte.
--
-- All CREATE OR REPLACE / additive — nothing in 0001..0012 is edited in place (append-only ledger).

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

-- 1. Moderation state for the photo-move queue -----------------------------------------------
-- text + CHECK (not a native enum) so the allowed states can evolve via a constraint swap without
-- ALTER TYPE locking. Constant default 'none' => metadata-only add on PG11+ (no table rewrite).
ALTER TABLE location_images
  ADD COLUMN IF NOT EXISTS apply_state text NOT NULL DEFAULT 'none'
  CHECK (apply_state IN ('none','pending_review','approved','rejected'));

-- Partial index over ONLY the pending rows — stays permanently tiny as approved/rejected rows fall
-- out of the predicate. The operator queue query MUST contain `WHERE apply_state = 'pending_review'`
-- literally for the planner to use this index.
CREATE INDEX IF NOT EXISTS location_images_pending_review_ix
  ON location_images (created_at) WHERE apply_state = 'pending_review';

-- Backfill: rows already auto-applied by 0012 are, retroactively, approved moves.
UPDATE location_images SET apply_state = 'approved' WHERE applied = true AND apply_state = 'none';

-- 2. recompute_image: 0012 body + two-band split --------------------------------------------
-- Reproduces the 0012 body VERBATIM (score/status/upvotes/downvotes + the GREATEST(3, ...) score
-- gate) and replaces ONLY 0012's single apply block with the Band A / Band B / else split.
-- v_dist is the actual move distance (metres, WGS84 spheroid) from the immutable origin; v_indep is
-- the count of DISTINCT INDEPENDENT helpful upvoters (excludes the photo's own submitter ip_hash).
CREATE OR REPLACE FUNCTION recompute_image(p_image_id bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_up int; v_dn int; v_score int; v_status image_status;
  v_lat double precision; v_lon double precision; v_applied boolean; v_loc bigint;
  v_sub_iph  text;
  v_eng      integer;
  v_req      integer;
  v_threshold integer;
  v_dist     double precision;
  v_indep    integer;
  v_old_lon  double precision;
  v_old_lat  double precision;
  -- Band constants. plpgsql cannot read app config, so these mirror config.py:
  --   250  = photo_auto_apply_move_m   (Band A auto-apply radius, metres)
  --   4    = photo_large_move_min_voters (Band B independent-upvoter floor)
  --   2000 = correction_max_move_m     (hard ceiling, metres)
  c_auto_apply_m  constant double precision := 250;
  c_min_voters    constant integer          := 4;
  c_max_move_m    constant double precision := 2000;
BEGIN
  SELECT count(*) FILTER (WHERE helpful), count(*) FILTER (WHERE NOT helpful)
    INTO v_up, v_dn FROM image_votes WHERE image_id = p_image_id;
  v_score := v_up - v_dn;
  -- score <= -2 hidden; >= 1 visible (vouched); else pending (new / no net votes)
  v_status := (CASE WHEN v_score <= -2 THEN 'hidden' WHEN v_score >= 1 THEN 'visible' ELSE 'pending' END)::image_status;

  UPDATE location_images
     SET upvotes = v_up, downvotes = v_dn, score = v_score, status = v_status
   WHERE id = p_image_id
   RETURNING suggested_lat, suggested_lon, applied, location_id, submitter_ip_hash
        INTO v_lat, v_lon, v_applied, v_loc, v_sub_iph;

  -- Community-validated pin correction: gate on score, then split by move distance (the two bands).
  IF v_lat IS NOT NULL AND v_lon IS NOT NULL AND NOT v_applied THEN
    v_eng := location_engagement(v_loc);
    v_req := correction_required_support(v_eng);   -- 1 (Cold) / 2 (Warm) / 4 (Hot)
    v_threshold := GREATEST(3, v_req);             -- preserve the historical >=3 floor; Hot needs >=4
    IF v_score >= v_threshold THEN
      -- Actual move distance from the immutable origin (metres, spheroid) and independent-upvoter count.
      SELECT ST_Distance(COALESCE(l.origin_geom, l.geom)::geography,
                         ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326)::geography),
             ST_X(l.geom), ST_Y(l.geom)
        INTO v_dist, v_old_lon, v_old_lat
        FROM locations l WHERE l.id = v_loc;

      SELECT count(DISTINCT ip_hash) FILTER (WHERE helpful AND ip_hash <> v_sub_iph)
        INTO v_indep FROM image_votes WHERE image_id = p_image_id;

      IF v_dist IS NULL THEN
        -- No location row / null geom: leave everything untouched.
        NULL;
      ELSIF v_dist <= c_auto_apply_m THEN
        -- BAND A: small move — auto-apply exactly like 0012 and record it as an approved move.
        UPDATE locations
           SET geom = ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326), updated_at = now()
         WHERE id = v_loc;
        INSERT INTO moderation_audit (location_id, kind, correction_id, field,
                                      prior_value, new_value, actor_ip_hash)
        VALUES (v_loc, 'pin_correction', p_image_id, NULL,
                jsonb_build_object('lon', v_old_lon, 'lat', v_old_lat),
                jsonb_build_object('lon', v_lon, 'lat', v_lat), v_sub_iph);
        UPDATE location_images SET applied = true, apply_state = 'approved' WHERE id = p_image_id;
      ELSIF v_dist <= c_max_move_m AND v_indep >= c_min_voters THEN
        -- BAND B: large-but-bounded move with enough independent support — HOLD for an operator.
        -- The pin is NOT moved; the row is queued (idempotent: only 'none' advances to pending).
        UPDATE location_images SET apply_state = 'pending_review'
         WHERE id = p_image_id AND apply_state = 'none';
      END IF;
      -- else (251 m..2 km with < 4 independent upvoters, or > 2 km): apply_state stays 'none';
      -- the photo still vouches per its score, the pin is untouched.
    END IF;
  END IF;
END; $$;

-- 3. apply_pending_image_move: operator commits a held (Band B) move ------------------------
-- Called by the /admin apply-move endpoint. Locks the image row, verifies it is still pending,
-- re-checks the 2 km origin cap (defence against a stale queue entry), then performs the SAME move
-- + SAME moderation_audit row 0012/Band A would have written, and marks the row approved.
-- Returns: 'not_pending' | 'too_far' | 'applied'.
CREATE OR REPLACE FUNCTION apply_pending_image_move(p_image_id bigint) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
  v_state    text;
  v_lat      double precision;
  v_lon      double precision;
  v_loc      bigint;
  v_sub_iph  text;
  v_dist     double precision;
  v_old_lon  double precision;
  v_old_lat  double precision;
  c_max_move_m constant double precision := 2000;   -- mirrors config.correction_max_move_m
BEGIN
  SELECT apply_state, suggested_lat, suggested_lon, location_id, submitter_ip_hash
    INTO v_state, v_lat, v_lon, v_loc, v_sub_iph
    FROM location_images WHERE id = p_image_id FOR UPDATE;

  IF v_state IS DISTINCT FROM 'pending_review' THEN
    RETURN 'not_pending';
  END IF;

  SELECT ST_Distance(COALESCE(l.origin_geom, l.geom)::geography,
                     ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326)::geography),
         ST_X(l.geom), ST_Y(l.geom)
    INTO v_dist, v_old_lon, v_old_lat
    FROM locations l WHERE l.id = v_loc;

  IF v_dist IS NULL OR v_dist > c_max_move_m THEN
    RETURN 'too_far';
  END IF;

  UPDATE locations
     SET geom = ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326), updated_at = now()
   WHERE id = v_loc;
  INSERT INTO moderation_audit (location_id, kind, correction_id, field,
                                prior_value, new_value, actor_ip_hash)
  VALUES (v_loc, 'pin_correction', p_image_id, NULL,
          jsonb_build_object('lon', v_old_lon, 'lat', v_old_lat),
          jsonb_build_object('lon', v_lon, 'lat', v_lat), v_sub_iph);
  UPDATE location_images SET applied = true, apply_state = 'approved' WHERE id = p_image_id;
  RETURN 'applied';
END; $$;

INSERT INTO schema_migrations (version) VALUES ('0013_photo_move_bands.sql')
  ON CONFLICT DO NOTHING;

COMMIT;

-- ============================================================================================
-- ROLLBACK (commented — apply by hand to undo 0013 and restore the 0012 behaviour):
--
-- BEGIN;
--
-- -- Restore the 0012 recompute_image body verbatim (single apply block, no bands).
-- CREATE OR REPLACE FUNCTION recompute_image(p_image_id bigint) RETURNS void LANGUAGE plpgsql AS $$
-- DECLARE
--   v_up int; v_dn int; v_score int; v_status image_status;
--   v_lat double precision; v_lon double precision; v_applied boolean; v_loc bigint;
--   v_sub_iph  text;
--   v_eng      integer;
--   v_req      integer;
--   v_threshold integer;
--   v_within   boolean;
--   v_old_lon  double precision;
--   v_old_lat  double precision;
-- BEGIN
--   SELECT count(*) FILTER (WHERE helpful), count(*) FILTER (WHERE NOT helpful)
--     INTO v_up, v_dn FROM image_votes WHERE image_id = p_image_id;
--   v_score := v_up - v_dn;
--   v_status := (CASE WHEN v_score <= -2 THEN 'hidden' WHEN v_score >= 1 THEN 'visible' ELSE 'pending' END)::image_status;
--   UPDATE location_images
--      SET upvotes = v_up, downvotes = v_dn, score = v_score, status = v_status
--    WHERE id = p_image_id
--    RETURNING suggested_lat, suggested_lon, applied, location_id, submitter_ip_hash
--         INTO v_lat, v_lon, v_applied, v_loc, v_sub_iph;
--   IF v_lat IS NOT NULL AND v_lon IS NOT NULL AND NOT v_applied THEN
--     v_eng := location_engagement(v_loc);
--     v_req := correction_required_support(v_eng);
--     v_threshold := GREATEST(3, v_req);
--     IF v_score >= v_threshold THEN
--       SELECT ST_DWithin(COALESCE(l.origin_geom, l.geom)::geography,
--                         ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326)::geography, 2000),
--              ST_X(l.geom), ST_Y(l.geom)
--         INTO v_within, v_old_lon, v_old_lat
--         FROM locations l WHERE l.id = v_loc;
--       IF COALESCE(v_within, false) THEN
--         UPDATE locations
--            SET geom = ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326), updated_at = now()
--          WHERE id = v_loc;
--         INSERT INTO moderation_audit (location_id, kind, correction_id, field,
--                                       prior_value, new_value, actor_ip_hash)
--         VALUES (v_loc, 'pin_correction', p_image_id, NULL,
--                 jsonb_build_object('lon', v_old_lon, 'lat', v_old_lat),
--                 jsonb_build_object('lon', v_lon, 'lat', v_lat), v_sub_iph);
--         UPDATE location_images SET applied = true WHERE id = p_image_id;
--       END IF;
--     END IF;
--   END IF;
-- END; $$;
--
-- DROP FUNCTION IF EXISTS apply_pending_image_move(bigint);
-- DROP INDEX IF EXISTS location_images_pending_review_ix;
-- ALTER TABLE location_images DROP COLUMN IF EXISTS apply_state;
-- DELETE FROM schema_migrations WHERE version = '0013_photo_move_bands.sql';
--
-- COMMIT;
-- ============================================================================================
