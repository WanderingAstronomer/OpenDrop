-- OpenDrop — crowd field corrections (migration 0009)
-- Source of truth: docs/DATA_MODEL.md (engagement-tiered trust model).
--
-- WHY THIS EXISTS
-- Until now the only editable property of a seeded location was its PIN (0006 pin corrections).
-- A name, type, owning org, or address baked in by a scraper was frozen — if the map said
-- "Donation Bin" but it's really "Goodwill Donation Center", or the street address was wrong,
-- nobody could fix it. This migration adds photo-free, text-field "corrections" for four fields:
--   name · org_type · org_name (the owning org/brand) · address (line/city/state/postal as a unit)
--
-- TRUST MODEL — identical engagement tiers to pin corrections (0006): a proposal auto-applies
-- once its support reaches correction_required_support(engagement). The ONE difference: GPS
-- weighting is meaningless for a text edit (standing next to a bin doesn't make you right about
-- its legal name), so every participant — submitter and each confirmer — counts as a flat 1.
-- Reject rule and superseding mirror recompute_correction. All write paths are Turnstile-gated
-- at the API layer, exactly like every other community write.
--
-- Append-only ledger: nothing in 0001..0008 is edited in place. location_engagement is REDEFINED
-- here (CREATE OR REPLACE) to also count field-correction participants, keeping "engagement = all
-- distinct people who touched this location in any way" coherent across both correction systems.

BEGIN;

-- 1. Which field a proposal targets ----------------------------------------
-- 'address' bundles line/city/state/postal: one proposal replaces the whole postal address, so a
-- partial fix can't leave the address internally inconsistent.
CREATE TYPE field_correction_field AS ENUM ('name', 'org_type', 'org_name', 'address');

-- 2. Field-change proposals -------------------------------------------------
CREATE TABLE field_corrections (
  id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id       bigint NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  field             field_correction_field NOT NULL,
  -- Scalar fields (name/org_type/org_name) use proposed_value; org_type holds a valid org_type
  -- enum key (validated at the API). 'address' fills the four proposed_* address columns instead.
  proposed_value    text,
  proposed_line     text,
  proposed_city     text,
  proposed_state    varchar(2),
  proposed_postal   text,
  note              text,
  submitter_ip_hash text    NOT NULL,
  turnstile_hash    text,
  confirmations     integer NOT NULL DEFAULT 0,   -- count of confirming OTHER voters (flat 1 each)
  rejections        integer NOT NULL DEFAULT 0,   -- count of "no" votes
  support           integer NOT NULL DEFAULT 0,   -- submitter(1) + confirmers (drives auto-apply)
  required_support  integer NOT NULL DEFAULT 1,   -- snapshot of the tier threshold at last recompute
  status            correction_status NOT NULL DEFAULT 'open',  -- reuse the 0006 ENUM
  applied           boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  applied_at        timestamptz,
  CONSTRAINT field_corr_state_format CHECK (proposed_state IS NULL OR proposed_state ~ '^[A-Z]{2}$')
);
CREATE INDEX field_corrections_loc_ix  ON field_corrections (location_id, status);
CREATE INDEX field_corrections_open_ix ON field_corrections (location_id) WHERE status = 'open';
-- One open proposal per (location, field, submitter) — re-proposing the same field is an update,
-- not a second row (keeps the support meter honest).
CREATE UNIQUE INDEX field_corrections_one_open
  ON field_corrections (location_id, field, submitter_ip_hash) WHERE status = 'open';

-- 3. Confirm / reject votes on a proposal -----------------------------------
CREATE TABLE field_correction_votes (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  correction_id  bigint  NOT NULL REFERENCES field_corrections(id) ON DELETE CASCADE,
  ip_hash        text    NOT NULL,
  confirm        boolean NOT NULL,                 -- true = "yes, this is right"; false = "no"
  turnstile_hash text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_field_correction_vote UNIQUE (correction_id, ip_hash)
);

-- 4. Fold field-correction participants into engagement ---------------------
-- Redefine 0006's location_engagement to ALSO count people who proposed/voted on field
-- corrections. Engagement only ever SCALES thresholds upward, so a vandal who brigades field
-- edits raises their own bar — never lowers it. Empty in every pre-0009 dataset, so this is a
-- no-op for existing pin-correction tiers until field corrections start arriving.
CREATE OR REPLACE FUNCTION location_engagement(p_location_id bigint)
RETURNS integer LANGUAGE sql STABLE AS $$
  SELECT count(DISTINCT h)::int FROM (
    SELECT ip_hash AS h        FROM votes               WHERE location_id = p_location_id
    UNION ALL
    SELECT submitter_ip_hash   FROM location_images     WHERE location_id = p_location_id
    UNION ALL
    SELECT iv.ip_hash          FROM image_votes iv
                               JOIN location_images li ON li.id = iv.image_id
                               WHERE li.location_id = p_location_id
    UNION ALL
    SELECT submitter_ip_hash   FROM location_corrections WHERE location_id = p_location_id
    UNION ALL
    SELECT cv.ip_hash          FROM correction_votes cv
                               JOIN location_corrections lc ON lc.id = cv.correction_id
                               WHERE lc.location_id = p_location_id
    UNION ALL
    SELECT ip_hash             FROM attribute_votes     WHERE location_id = p_location_id
    UNION ALL
    SELECT submitter_ip_hash   FROM field_corrections   WHERE location_id = p_location_id
    UNION ALL
    SELECT fcv.ip_hash         FROM field_correction_votes fcv
                               JOIN field_corrections fc ON fc.id = fcv.correction_id
                               WHERE fc.location_id = p_location_id
  ) s WHERE h IS NOT NULL;
$$;

-- 5. Field-correction consensus + auto-apply --------------------------------
-- Mirrors recompute_correction (0007) with flat weights (no GPS) and no distance cap. On apply it
-- writes the proposed value into the matching location column(s) and supersedes other open
-- proposals FOR THE SAME FIELD (a name fix doesn't moot an address fix). org_type's value is a
-- valid enum key (the API validates before insert; the ::org_type cast is the backstop).
CREATE OR REPLACE FUNCTION recompute_field_correction(p_correction_id bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_loc     bigint;
  v_field   field_correction_field;
  v_val     text;
  v_line    text;
  v_city    text;
  v_state   varchar(2);
  v_postal  text;
  v_applied boolean;
  v_status  correction_status;
  v_eng     integer;
  v_req     integer;
  v_conf    integer;   -- count of confirming OTHER voters (flat 1 each)
  v_reject  integer;   -- count of "no" votes
  v_support integer;   -- submitter(1) + confirmers
BEGIN
  SELECT location_id, field, proposed_value, proposed_line, proposed_city,
         proposed_state, proposed_postal, applied, status
    INTO v_loc, v_field, v_val, v_line, v_city, v_state, v_postal, v_applied, v_status
  FROM field_corrections WHERE id = p_correction_id;

  IF v_loc IS NULL THEN RETURN; END IF;

  SELECT COALESCE(SUM(CASE WHEN confirm THEN 1 ELSE 0 END), 0),
         COALESCE(SUM(CASE WHEN NOT confirm THEN 1 ELSE 0 END), 0)
    INTO v_conf, v_reject
  FROM field_correction_votes WHERE correction_id = p_correction_id;

  v_support := 1 + v_conf;  -- submitter contributes a flat 1; no GPS weighting for text fields
  v_eng := location_engagement(v_loc);
  v_req := correction_required_support(v_eng);

  UPDATE field_corrections
     SET confirmations = v_conf, rejections = v_reject,
         support = v_support, required_support = v_req
   WHERE id = p_correction_id;

  -- Already resolved? recompute is idempotent — stop here.
  IF v_applied OR v_status <> 'open' THEN RETURN; END IF;

  -- Reject when clearly out-voted (>=2 rejects and rejects strictly lead the confirms).
  IF v_reject >= 2 AND v_reject > v_conf THEN
    UPDATE field_corrections SET status = 'rejected' WHERE id = p_correction_id;
    RETURN;
  END IF;

  -- Auto-apply once support reaches the tier threshold.
  IF v_support >= v_req THEN
    IF v_field = 'name' AND v_val IS NOT NULL AND length(btrim(v_val)) > 0 THEN
      UPDATE locations SET name = v_val, updated_at = now() WHERE id = v_loc;
    ELSIF v_field = 'org_type' AND v_val IS NOT NULL THEN
      UPDATE locations SET org_type = v_val::org_type, updated_at = now() WHERE id = v_loc;
    ELSIF v_field = 'org_name' THEN
      UPDATE locations SET org_name = NULLIF(btrim(v_val), ''), updated_at = now() WHERE id = v_loc;
    ELSIF v_field = 'address' THEN
      UPDATE locations
         SET address_line = v_line,
             house_number = normalize_house_number(v_line),
             city = v_city, state = v_state, postal_code = v_postal,
             updated_at = now()
       WHERE id = v_loc;
    END IF;

    UPDATE field_corrections
       SET status = 'applied', applied = true, applied_at = now()
     WHERE id = p_correction_id;
    -- Other open proposals for the SAME field are now moot.
    UPDATE field_corrections
       SET status = 'superseded'
     WHERE location_id = v_loc AND field = v_field AND id <> p_correction_id AND status = 'open';
  END IF;
END; $$;

-- After-insert: a Cold (good-faith) proposal applies immediately here.
CREATE OR REPLACE FUNCTION trg_after_field_correction() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM recompute_field_correction(NEW.id);
  RETURN NULL;
END; $$;

CREATE TRIGGER field_corrections_after_insert
  AFTER INSERT ON field_corrections
  FOR EACH ROW EXECUTE FUNCTION trg_after_field_correction();

CREATE OR REPLACE FUNCTION trg_after_field_correction_vote() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_id bigint := COALESCE(NEW.correction_id, OLD.correction_id);
BEGIN
  PERFORM recompute_field_correction(v_id);
  RETURN NULL;
END; $$;

CREATE TRIGGER field_correction_votes_after_write
  AFTER INSERT OR UPDATE OR DELETE ON field_correction_votes
  FOR EACH ROW EXECUTE FUNCTION trg_after_field_correction_vote();

INSERT INTO schema_migrations (version) VALUES ('0009_field_corrections.sql') ON CONFLICT DO NOTHING;

COMMIT;
