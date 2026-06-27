-- 0004: community photos + photo-validated pin-accuracy corrections.
-- A photo may carry a suggested corrected location; when its helpful-vote score crosses
-- the apply threshold, the canonical pin is moved automatically (no manual moderation).
BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TYPE image_status AS ENUM ('pending', 'visible', 'hidden');

CREATE TABLE location_images (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id       bigint NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  path              text   NOT NULL,            -- filename under the media dir; served at /media/<path>
  mime              text   NOT NULL,
  submitter_ip_hash text   NOT NULL,
  turnstile_hash    text,
  suggested_lat     double precision,           -- set => this photo proposes a corrected pin
  suggested_lon     double precision,
  upvotes           integer NOT NULL DEFAULT 0,
  downvotes         integer NOT NULL DEFAULT 0,
  score             integer NOT NULL DEFAULT 0,
  status            image_status NOT NULL DEFAULT 'pending',
  applied           boolean NOT NULL DEFAULT false,  -- whether the suggested correction was applied
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX location_images_loc_ix ON location_images (location_id, status, score DESC);

CREATE TABLE image_votes (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  image_id   bigint  NOT NULL REFERENCES location_images(id) ON DELETE CASCADE,
  ip_hash    text    NOT NULL,
  helpful    boolean NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_image_vote UNIQUE (image_id, ip_hash)
);

-- Recompute a photo's score/status; auto-apply a pin correction once vouched enough.
CREATE OR REPLACE FUNCTION recompute_image(p_image_id bigint) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_up int; v_dn int; v_score int; v_status image_status;
  v_lat double precision; v_lon double precision; v_applied boolean; v_loc bigint;
BEGIN
  SELECT count(*) FILTER (WHERE helpful), count(*) FILTER (WHERE NOT helpful)
    INTO v_up, v_dn FROM image_votes WHERE image_id = p_image_id;
  v_score := v_up - v_dn;
  -- score <= -2 hidden; >= 1 visible (vouched); else pending (new / no net votes)
  v_status := (CASE WHEN v_score <= -2 THEN 'hidden' WHEN v_score >= 1 THEN 'visible' ELSE 'pending' END)::image_status;

  UPDATE location_images
     SET upvotes = v_up, downvotes = v_dn, score = v_score, status = v_status
   WHERE id = p_image_id
   RETURNING suggested_lat, suggested_lon, applied, location_id INTO v_lat, v_lon, v_applied, v_loc;

  -- Community-validated pin correction (apply once, at score >= 3)
  IF v_lat IS NOT NULL AND v_lon IS NOT NULL AND NOT v_applied AND v_score >= 3 THEN
    UPDATE locations SET geom = ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326), updated_at = now()
     WHERE id = v_loc;
    UPDATE location_images SET applied = true WHERE id = p_image_id;
  END IF;
END; $$;

CREATE OR REPLACE FUNCTION trg_after_image_vote() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_id bigint := COALESCE(NEW.image_id, OLD.image_id);
BEGIN
  PERFORM recompute_image(v_id);
  RETURN NULL;
END; $$;

CREATE TRIGGER image_votes_after_write
  AFTER INSERT OR UPDATE OR DELETE ON image_votes
  FOR EACH ROW EXECUTE FUNCTION trg_after_image_vote();

INSERT INTO schema_migrations (version) VALUES ('0004_images.sql') ON CONFLICT DO NOTHING;

COMMIT;
