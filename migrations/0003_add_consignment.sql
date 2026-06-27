-- 0003: add the 'consignment' org_type (resale / buy-back shops — sell, don't just donate).
-- ALTER TYPE ... ADD VALUE is allowed inside a transaction in PG12+ (the value just can't be
-- USED in the same transaction; we only add it here).
BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

ALTER TYPE org_type ADD VALUE IF NOT EXISTS 'consignment' AFTER 'thrift_store';

INSERT INTO schema_migrations (version) VALUES ('0003_add_consignment.sql') ON CONFLICT DO NOTHING;

COMMIT;
