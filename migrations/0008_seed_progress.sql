-- OpenDrop — national seed checkpoint table (migration 0008)
--
-- The national seeder (pipeline/seed_national.py) walks all 50 states + DC, running the scrapers
-- once per state. That is a long, gentle, overnight-scale job, so it must be RESUMABLE: if it is
-- interrupted (Ctrl-C, container restart, a crash at state #37) it should pick up where it left
-- off rather than re-sweeping everything. This table is that durable checkpoint — one row per
-- region the seeder has touched, plus a synthetic row for the final dedup/promote step.
--
-- Append-only ledger: nothing in earlier migrations is edited. Idempotent (IF NOT EXISTS) so a
-- re-run is a no-op.

BEGIN;

CREATE TABLE IF NOT EXISTS seed_progress (
  region_name text PRIMARY KEY,                 -- e.g. 'oh', 'ca', or the synthetic '__finalize__'
  status      text NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  started_at  timestamptz,
  finished_at timestamptz,
  detail      jsonb NOT NULL DEFAULT '{}'::jsonb,   -- per-scraper counts / last error
  updated_at  timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT seed_progress_status_chk CHECK (status IN ('pending', 'running', 'done', 'failed'))
);

-- Cheap lookup of "what's still outstanding" on resume.
CREATE INDEX IF NOT EXISTS seed_progress_status_ix ON seed_progress (status);

INSERT INTO schema_migrations (version) VALUES ('0008_seed_progress.sql') ON CONFLICT DO NOTHING;

COMMIT;
