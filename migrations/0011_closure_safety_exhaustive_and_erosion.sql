-- OpenDrop — closure-detection safety (migration 0011)
--
-- Why: closure/deletion detection retired ~633 REAL bins on live. Two blind spots caused it:
--   (1) A *clean* run of a NON-exhaustive source (Planet Aid nearest-20 grid; Salvation Army /
--       USAgain ZIP-radius sweeps) legitimately omits real bins it didn't sample — yet reconcile
--       treated "absent from this run" as "closed" and deleted the link.
--   (2) The per-run 40% circuit breaker is blind to *sub-40%* bleed that accumulates across many
--       runs (Planet Aid eroded 643 links across 53 regional seed runs, each under the threshold).
--
-- Two guards land here:
--   1. sources.fetch_is_exhaustive — only a source whose fetch enumerates EVERY in-region record
--      (OSM Overpass bbox) may reconcile. Default false: a new source never retires until it is
--      explicitly proven exhaustive.
--   2. reconcile_audit — a per source+region ledger of every reconcile attempt, so a cumulative
--      cross-run erosion breaker can refuse a slow bleed the single-run breaker cannot see.

BEGIN;

ALTER TABLE sources
  ADD COLUMN IF NOT EXISTS fetch_is_exhaustive boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN sources.fetch_is_exhaustive IS
  'True only if fetch(region) enumerates EVERY in-region record, so an absent source_ref genuinely means closed. Closure detection is gated on this; nearest-N / radius / grid-sampled feeds are NOT exhaustive and must never reconcile.';

-- OSM Overpass returns every matching node in the queried bbox -> exhaustive.
-- planet_aid (nearest-20 grid), salvation_army / usagain (ZIP-radius nearest-N),
-- wearable_collections (geocode-dependent fixed list), crowd (manual) -> NOT exhaustive.
UPDATE sources SET fetch_is_exhaustive = true  WHERE code = 'osm';
UPDATE sources SET fetch_is_exhaustive = false WHERE code <> 'osm';

-- Per source+region ledger of reconcile attempts (executed or skipped). The cumulative breaker
-- sums `retired` over a rolling window and refuses to retire more than a fraction of the region's
-- high-water-mark link count — catching slow erosion the single-run breaker waves through.
CREATE TABLE IF NOT EXISTS reconcile_audit (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  source_code    text    NOT NULL REFERENCES sources(code),
  region_key     text    NOT NULL,              -- Region.name (stable per-region identifier)
  baseline       integer NOT NULL,              -- in-region links present BEFORE this reconcile
  would_retire   integer NOT NULL,              -- links this run proposed to retire
  retired        integer NOT NULL DEFAULT 0,    -- links actually deleted (0 when skipped)
  seen_count     integer NOT NULL DEFAULT 0,    -- records the run actually saw in-region
  executed       boolean NOT NULL DEFAULT false,
  reason         text,                          -- NULL when executed; else the skip reason code
  run_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS reconcile_audit_src_region_ix
  ON reconcile_audit (source_code, region_key, run_at DESC);

INSERT INTO schema_migrations (version) VALUES ('0011_closure_safety_exhaustive_and_erosion.sql')
  ON CONFLICT DO NOTHING;

COMMIT;
