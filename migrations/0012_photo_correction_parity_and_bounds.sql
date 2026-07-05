-- OpenDrop — photo pin-correction parity + coordinate bounds (migration 0012)
--
-- WHY THIS EXISTS
-- Pre-production security audit (2026-07-05) found the PHOTO pin-correction path (0004
-- recompute_image) bypassed every safety control built for the drag-pin path. When a photo carrying
-- suggested_lat/suggested_lon reached helpful-score >= 3 it moved locations.geom with:
--   * NO origin-anchored 2 km distance cap (the pin-walk / relocation guard added in 0007),
--   * NO engagement tier (a Hot, heavily-used charity was as easy to move as a fresh pin),
--   * NO moderation_audit row (the move was silent and UN-revertible by the operator tools), and
--   * NO coordinate bounds (suggested_lat/lon accepted NaN/Inf/out-of-range, corrupting geom).
-- migration 0005's own header warned "a script that mass-upvotes a malicious correction could
-- silently relocate a location" — the 0006/0007/0010 hardening only ever touched recompute_correction.
--
-- This migration brings the photo path to parity:
--   1. DB-level CHECK bounds on location_images.suggested_lat/lon (parity with location_corrections'
--      correction_lat_range/lon_range). Existing out-of-range/NaN rows are sanitized to NULL first.
--   2. recompute_image now gates the auto-apply on the engagement-tiered support threshold AND the
--      origin-anchored 2 km ST_DWithin cap, and writes a moderation_audit row so the move is
--      attributable and revertible exactly like a drag-pin correction. The historical floor of 3
--      helpful votes is preserved as a minimum (a Hot location now needs >= 4, matching the tier).
--
-- All CREATE OR REPLACE / additive — nothing in 0001..0011 is edited in place (append-only ledger).

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

-- 1. Bound the photo-suggested coordinates at the DB layer -----------------------------------
-- Sanitize any pre-existing out-of-range/NaN rows to NULL FIRST (both columns together, preserving
-- the both-or-neither invariant the API enforces) so the validated ADD CONSTRAINT succeeds. NaN
-- fails a BETWEEN test, so the NOT(...) predicate catches it too.
UPDATE location_images
   SET suggested_lat = NULL, suggested_lon = NULL
 WHERE suggested_lat IS NOT NULL
   AND NOT (suggested_lat BETWEEN -90 AND 90 AND suggested_lon BETWEEN -180 AND 180);

ALTER TABLE location_images DROP CONSTRAINT IF EXISTS image_suggested_lat_range;
ALTER TABLE location_images DROP CONSTRAINT IF EXISTS image_suggested_lon_range;
ALTER TABLE location_images ADD CONSTRAINT image_suggested_lat_range
  CHECK (suggested_lat IS NULL OR suggested_lat BETWEEN -90  AND 90);
ALTER TABLE location_images ADD CONSTRAINT image_suggested_lon_range
  CHECK (suggested_lon IS NULL OR suggested_lon BETWEEN -180 AND 180);

-- 2. Photo-correction auto-apply: tier + origin-anchored distance cap + audit trail ----------
-- Reproduces the 0004 body verbatim EXCEPT the apply block, which now mirrors recompute_correction
-- (0010): engagement-tiered threshold, 2 km cap measured from the immutable origin, and a
-- moderation_audit row (kind='pin_correction'; correction_id carries the location_images.id) so the
-- operator revert / revert-actor / revert-all tooling can undo a photo-driven move too.
CREATE OR REPLACE FUNCTION recompute_image(p_image_id bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_up int; v_dn int; v_score int; v_status image_status;
  v_lat double precision; v_lon double precision; v_applied boolean; v_loc bigint;
  v_sub_iph  text;
  v_eng      integer;
  v_req      integer;
  v_threshold integer;
  v_within   boolean;
  v_old_lon  double precision;
  v_old_lat  double precision;
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

  -- Community-validated pin correction: apply once, bounded by the SAME guards as the drag-pin path.
  IF v_lat IS NOT NULL AND v_lon IS NOT NULL AND NOT v_applied THEN
    v_eng := location_engagement(v_loc);
    v_req := correction_required_support(v_eng);   -- 1 (Cold) / 2 (Warm) / 4 (Hot)
    v_threshold := GREATEST(3, v_req);             -- preserve the historical >=3 floor; Hot needs >=4
    IF v_score >= v_threshold THEN
      SELECT ST_DWithin(COALESCE(l.origin_geom, l.geom)::geography,
                        ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326)::geography, 2000),
             ST_X(l.geom), ST_Y(l.geom)
        INTO v_within, v_old_lon, v_old_lat
        FROM locations l WHERE l.id = v_loc;

      IF COALESCE(v_within, false) THEN
        UPDATE locations
           SET geom = ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326), updated_at = now()
         WHERE id = v_loc;
        INSERT INTO moderation_audit (location_id, kind, correction_id, field,
                                      prior_value, new_value, actor_ip_hash)
        VALUES (v_loc, 'pin_correction', p_image_id, NULL,
                jsonb_build_object('lon', v_old_lon, 'lat', v_old_lat),
                jsonb_build_object('lon', v_lon, 'lat', v_lat), v_sub_iph);
        UPDATE location_images SET applied = true WHERE id = p_image_id;
      END IF;
    END IF;
  END IF;
END; $$;

INSERT INTO schema_migrations (version) VALUES ('0012_photo_correction_parity_and_bounds.sql')
  ON CONFLICT DO NOTHING;

COMMIT;
