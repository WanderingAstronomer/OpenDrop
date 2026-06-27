-- Least-privilege application DB role for PRODUCTION (blast-radius reduction).
-- The default setup runs the API as the schema-owning role (full DDL). In prod, run the
-- *API* as this restricted role instead, and keep the owner role only for migrations and
-- the pipeline/scheduler (which need DELETE for closure-detection and DDL for migrations).
--
-- Apply ONCE, connected as the owner, supplying a strong password:
--   psql "$OWNER_DATABASE_URL" -v app_pw="$(openssl rand -hex 24)" -f deploy/app_role.sql
-- Then set the API's DATABASE_URL to:  postgresql://opendrop_app:<app_pw>@db:5432/opendrop
-- (Leave the scheduler/pipeline DATABASE_URL on the owner role.)

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'opendrop_app') THEN
    EXECUTE format('CREATE ROLE opendrop_app LOGIN PASSWORD %L', :'app_pw');
  ELSE
    EXECUTE format('ALTER ROLE opendrop_app LOGIN PASSWORD %L', :'app_pw');
  END IF;
END $$;

GRANT CONNECT ON DATABASE opendrop TO opendrop_app;
GRANT USAGE ON SCHEMA public TO opendrop_app;

-- Reads
GRANT SELECT ON locations, sources, location_sources, votes, pending_locations,
                scrape_log, v_public_locations TO opendrop_app;
-- Writes the API actually performs (votes, submissions, promotion, trigger recompute).
-- Deliberately NO DELETE and NO DDL.
GRANT INSERT ON votes, pending_locations, locations, location_sources TO opendrop_app;
GRANT UPDATE ON locations, pending_locations TO opendrop_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO opendrop_app;
