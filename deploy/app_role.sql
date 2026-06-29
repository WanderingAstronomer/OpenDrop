-- Least-privilege application DB role for PRODUCTION (blast-radius reduction).
-- The default setup runs the API as the schema-owning role (full DDL). In prod, run the
-- *API* as this restricted role instead, and keep the owner role only for migrations and
-- the pipeline/scheduler (which need DELETE for closure-detection and DDL for migrations).
--
-- Apply ONCE, connected as the owner, supplying a strong password:
--   psql "$OWNER_DATABASE_URL" -v app_pw="$(openssl rand -hex 24)" -f deploy/app_role.sql
-- Then set the API's DATABASE_URL to:  postgresql://opendrop_app:<app_pw>@db:5432/opendrop
-- (Leave the scheduler/pipeline DATABASE_URL on the owner role.)

-- Create-or-update the role with the supplied password, idempotently. We build the DDL at the psql
-- level (NOT inside a DO $$...$$ block) because psql does not interpolate :'app_pw' inside a
-- dollar-quoted string — doing so there yields "syntax error at or near :". \gexec runs whichever
-- single row the WHERE clause produces: CREATE on first apply, ALTER on re-apply.
SELECT format('CREATE ROLE opendrop_app LOGIN PASSWORD %L', :'app_pw')
  WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opendrop_app')
\gexec
SELECT format('ALTER ROLE opendrop_app LOGIN PASSWORD %L', :'app_pw')
  WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opendrop_app')
\gexec

GRANT CONNECT ON DATABASE opendrop TO opendrop_app;
GRANT USAGE ON SCHEMA public TO opendrop_app;

-- Reads — core (0001) plus every community + moderation table the API reads back (0004..0010).
-- Without the community grants, EVERY photo/correction/rating/field-correction/report call fails
-- "permission denied" when the API runs as this role (the documented prod path).
-- schema_migrations is READ at boot by the schema-at-head guard (main.py): without SELECT here, the
-- guard can't read the ledger and silently fails open (logs a warning, skips the check) under this
-- exact prod role — so the version assertion we rely on for safe deploys would be a no-op.
GRANT SELECT ON locations, sources, location_sources, votes, pending_locations,
                scrape_log, v_public_locations, schema_migrations,
                location_images, image_votes,
                location_corrections, correction_votes, attribute_votes,
                field_corrections, field_correction_votes,
                moderation_audit, content_reports TO opendrop_app;

-- Writes the API actually performs (votes, submissions, promotion, photos, corrections, ratings,
-- field edits, reports, and the operator moderation actions). Trigger functions run as the
-- INVOKER (this role), so the consensus recompute's UPDATEs and its moderation_audit INSERT must
-- be permitted here too. Deliberately NO DDL.
GRANT INSERT ON votes, pending_locations, locations, location_sources,
                location_images, image_votes,
                location_corrections, correction_votes, attribute_votes,
                field_corrections, field_correction_votes,
                moderation_audit, content_reports TO opendrop_app;

-- UPDATE: locations/pending (promotion + recompute + operator takedown), every *_votes table that
-- upserts via ON CONFLICT DO UPDATE, the correction tables the triggers recompute, location_images
-- (photo takedown sets removed_at), and the two moderation queues (revert / resolve).
GRANT UPDATE ON locations, pending_locations,
                image_votes, correction_votes, attribute_votes, field_correction_votes,
                location_images, location_corrections, field_corrections,
                moderation_audit, content_reports TO opendrop_app;

-- DELETE is granted ONLY on attribute_votes (the "clear my rating" deselect). Photo takedown is a
-- soft-delete (UPDATE removed_at + unlink the file), so the API never needs row DELETE elsewhere.
GRANT DELETE ON attribute_votes TO opendrop_app;

-- Identity-column sequences for every grantable table (covers all tables created through 0010).
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO opendrop_app;
