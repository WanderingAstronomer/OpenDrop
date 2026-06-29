-- OpenDrop pre-cutover blast-radius report  (READ-ONLY — runs no writes).
--
-- Run this against the LIVE database before a gated cutover to quantify (a) how far the schema is
-- behind the code, and (b) how much live data the new behaviour touches — especially migration
-- 0010's authoritative-source threshold gate. Every table reference is guarded with to_regclass and
-- executed dynamically, so this file is safe to run against an OLD schema that doesn't yet have the
-- corrections / moderation tables: missing tables report "(table absent — pre-migration)".
--
--   docker compose exec -T db psql -U opendrop -d opendrop -f /dev/stdin < scripts/blast_radius.sql
--   # or:  psql "$DATABASE_URL" -f scripts/blast_radius.sql
--
-- Output is RAISE NOTICE lines (look under "psql:...: NOTICE:").

\set ON_ERROR_STOP on
DO $$
DECLARE
  n            bigint;
  n2           bigint;
  head         text;
BEGIN
  RAISE NOTICE '====================== OpenDrop blast-radius ======================';

  -- 1. Schema staleness: how far is this DB behind the migration set on disk?
  IF to_regclass('public.schema_migrations') IS NOT NULL THEN
    EXECUTE 'SELECT count(*), max(version) FROM schema_migrations' INTO n, head;
    RAISE NOTICE 'schema_migrations: % applied, head = %', n, head;
    RAISE NOTICE '  (code expects 0010_moderation_audit_and_thresholds.sql — anything less is behind)';
  ELSE
    RAISE NOTICE 'schema_migrations: ABSENT — this DB predates the migration ledger.';
  END IF;

  -- 2. Location inventory by status.
  IF to_regclass('public.locations') IS NOT NULL THEN
    EXECUTE 'SELECT count(*) FROM locations' INTO n;
    RAISE NOTICE 'locations: % total', n;
    FOR head, n IN EXECUTE
      'SELECT rpad(status::text, 10), count(*) FROM locations GROUP BY status ORDER BY 2 DESC'
    LOOP
      RAISE NOTICE '  status % : %', head, n;
    END LOOP;
  ELSE
    RAISE NOTICE 'locations: ABSENT (unexpected).';
  END IF;

  -- 3. Authoritative vs crowd-only — the population the 0010 gate protects.
  --    "Authoritative" = has at least one non-'crowd' source link.
  IF to_regclass('public.location_sources') IS NOT NULL THEN
    EXECUTE $q$
      SELECT
        count(*) FILTER (WHERE auth),
        count(*) FILTER (WHERE NOT auth)
      FROM (
        SELECT l.id, bool_or(s.source_code <> 'crowd') AS auth
        FROM locations l
        JOIN location_sources s ON s.location_id = l.id
        GROUP BY l.id
      ) t
    $q$ INTO n, n2;
    RAISE NOTICE 'gate scope (0010): % authoritatively-sourced locations — name/org/address edits', n;
    RAISE NOTICE '  now need >=1 confirmer even when Cold. % crowd-only keep old behaviour.', n2;
  ELSE
    RAISE NOTICE 'location_sources: ABSENT — cannot compute authoritative gate scope.';
  END IF;

  -- 4. Existing community signal volume (only present post-0006/0009).
  IF to_regclass('public.field_corrections') IS NOT NULL THEN
    EXECUTE 'SELECT count(*) FROM field_corrections' INTO n;
    RAISE NOTICE 'field_corrections: % rows (history retained; gate affects only FUTURE proposals)', n;
  ELSE
    RAISE NOTICE 'field_corrections: (table absent — pre-migration)';
  END IF;

  IF to_regclass('public.corrections') IS NOT NULL THEN
    EXECUTE 'SELECT count(*) FROM corrections' INTO n;
    RAISE NOTICE 'pin corrections: % rows', n;
  ELSE
    RAISE NOTICE 'corrections: (table absent — pre-migration)';
  END IF;

  -- 5. New 0010 surfaces — should be empty/zero immediately after cutover.
  IF to_regclass('public.moderation_audit') IS NOT NULL THEN
    EXECUTE 'SELECT count(*) FROM moderation_audit' INTO n;
    RAISE NOTICE 'moderation_audit: % rows (audit trail; starts empty, fills as corrections apply)', n;
  ELSE
    RAISE NOTICE 'moderation_audit: (table absent — 0010 not applied yet)';
  END IF;

  IF to_regclass('public.content_reports') IS NOT NULL THEN
    EXECUTE 'SELECT count(*) FROM content_reports' INTO n;
    RAISE NOTICE 'content_reports: % rows', n;
  ELSE
    RAISE NOTICE 'content_reports: (table absent — 0010 not applied yet)';
  END IF;

  -- 6. Media footprint (photos that a restore must also carry).
  IF to_regclass('public.location_images') IS NOT NULL THEN
    EXECUTE 'SELECT count(*) FROM location_images' INTO n;
    RAISE NOTICE 'location_images: % rows (back up the media volume alongside the DB)', n;
  ELSE
    RAISE NOTICE 'location_images: (table absent — pre-migration)';
  END IF;

  RAISE NOTICE '===================================================================';
END $$;
