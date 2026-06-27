-- 0002: fix recompute_confidence source-component for source-less locations.
-- LEAST/GREATEST ignore NULLs, so LEAST(85, SUM(...)) returned 85 (not 0) when a
-- location had zero ingest sources. This was latent until closure-detection began
-- removing a location's last source. Fix: COALESCE(SUM,0) INSIDE LEAST.
-- Also recompute existing rows so any source-less location is corrected immediately.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

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

-- Correct any rows affected by the old formula (esp. source-less ones left 'active').
DO $$
DECLARE r record;
BEGIN
  FOR r IN SELECT id FROM locations WHERE status NOT IN ('merged','hidden') LOOP
    PERFORM recompute_confidence(r.id);
  END LOOP;
END $$;

INSERT INTO schema_migrations (version) VALUES ('0002_fix_source_component.sql') ON CONFLICT DO NOTHING;

COMMIT;
