-- 0005: bot-protect photo ("image") votes with the same Turnstile gate the location vote and
-- photo upload already use. A photo's helpful-votes drive an AUTOMATIC pin correction
-- (recompute_image() moves the canonical pin once score >= 3), so a script that mass-upvotes a
-- malicious correction could silently relocate a location. Record the solved-token hash here,
-- mirroring votes.turnstile_hash and location_images.turnstile_hash.
BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE image_votes ADD COLUMN IF NOT EXISTS turnstile_hash text;

INSERT INTO schema_migrations (version) VALUES ('0005_image_vote_turnstile.sql') ON CONFLICT DO NOTHING;

COMMIT;
