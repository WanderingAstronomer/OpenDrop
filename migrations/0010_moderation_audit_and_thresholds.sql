-- OpenDrop — moderation, audit/revert ledger, public reports, anti-abuse thresholds (migration 0010)
-- Source of truth: docs/DATA_MODEL.md (engagement-tiered trust model) + docs/RUNBOOK.md.
--
-- WHY THIS EXISTS
-- Production hardening. The crowd correction system (0006/0009) auto-applies edits with NO record
-- of what it overwrote and NO way for an operator to undo a bad apply, and a lone good-faith
-- submitter can rename or re-address an authoritatively-sourced charity on a Cold location. There
-- is also no takedown path for an abusive photo or location. This migration adds, all additive /
-- CREATE OR REPLACE (the ledger is append-only — nothing in 0001..0009 is edited in place):
--
--   (1) moderation_audit  — append-only "what changed, from what, by whom" for every auto-applied
--       pin/field correction, so an operator can one-click or bulk REVERT (and revert everything a
--       single bad actor's ip_hash applied).
--   (2) content_reports   — public "report this" queue for a location or photo (operator triage).
--   (3) takedown columns  — location_images.removed_at/removed_reason (operator photo takedown,
--       soft-delete so the gallery never serves it and the file is unlinked) and
--       locations.takedown_reason/takedown_at (operator location takedown via status='hidden').
--   (4) THRESHOLD GATE    — recompute_field_correction now requires >=1 independent confirmer
--       (support>=2) before auto-applying name/org_name/address on an AUTHORITATIVELY-SOURCED
--       location (one carrying a non-'crowd' seed source). org_type, pin moves, and crowd-only
--       locations keep the normal tiered threshold. This is the "light" abuse stance: keep
--       auto-apply, but identity-critical edits to real charities can't land on one voice.
--
-- This migration deliberately performs NO status re-evaluation / backfill: the threshold change
-- affects only FUTURE corrections, and the audit insert only fires on FUTURE applies, so applying
-- 0010 to the live 15k-row dataset cannot flip the status of any existing location.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version    text PRIMARY KEY,
  applied_at timestamptz NOT NULL DEFAULT now()
);

-- 1. Audit / revert ledger ---------------------------------------------------
-- One row per auto-applied correction, capturing the column value(s) it overwrote (prior_value)
-- and what it wrote (new_value) as jsonb, plus the submitter's ip_hash so an operator can revert
-- every change one actor pushed. reverted_at is set when an operator undoes it (the row is kept).
CREATE TABLE moderation_audit (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  location_id   bigint NOT NULL REFERENCES locations(id) ON DELETE CASCADE,
  kind          text   NOT NULL CHECK (kind IN ('field_correction', 'pin_correction')),
  correction_id bigint,                         -- field_corrections.id or location_corrections.id
  field         text,                           -- for field corrections: name/org_type/org_name/address
  prior_value   jsonb  NOT NULL,                -- snapshot of changed column(s) BEFORE the apply
  new_value     jsonb  NOT NULL,                -- snapshot AFTER the apply
  actor_ip_hash text,                           -- submitter of the applied proposal
  applied_at    timestamptz NOT NULL DEFAULT now(),
  reverted_at   timestamptz,                    -- set when an operator reverts; row is retained
  reverted_note text
);
CREATE INDEX moderation_audit_loc_ix    ON moderation_audit (location_id, applied_at DESC);
CREATE INDEX moderation_audit_actor_ix  ON moderation_audit (actor_ip_hash) WHERE reverted_at IS NULL;
CREATE INDEX moderation_audit_active_ix ON moderation_audit (applied_at DESC) WHERE reverted_at IS NULL;

-- 2. Public report queue -----------------------------------------------------
-- A "report this location / photo" filed by an anonymous visitor (Turnstile-gated, rate-limited at
-- the API). Reporting does NOT auto-remove a location (that would let one actor nuke seed pins);
-- it files a complaint for operator triage. Reporting a PHOTO additionally hides it from the
-- default gallery at the API layer (reversible) — erring toward hiding user-generated media.
CREATE TABLE content_reports (
  id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  target_type      text NOT NULL CHECK (target_type IN ('location', 'image')),
  target_id        bigint NOT NULL,
  reason           text,
  reporter_ip_hash text NOT NULL,
  turnstile_hash   text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  resolved_at      timestamptz,
  resolved_note    text
);
CREATE INDEX content_reports_open_ix   ON content_reports (created_at DESC) WHERE resolved_at IS NULL;
CREATE INDEX content_reports_ip_ix     ON content_reports (reporter_ip_hash, created_at);
CREATE INDEX content_reports_target_ix ON content_reports (target_type, target_id);

-- 3. Takedown columns --------------------------------------------------------
-- Photos: soft-delete. An operator takedown sets removed_at (+reason) and unlinks the file from
-- the media volume. list_images filters removed_at IS NOT NULL out of BOTH the default and the
-- include_low galleries, so a removed photo is unreachable via the API; the unlinked file makes
-- the raw /media/<path> 404 too. The row is kept as the takedown record.
ALTER TABLE location_images ADD COLUMN IF NOT EXISTS removed_at     timestamptz;
ALTER TABLE location_images ADD COLUMN IF NOT EXISTS removed_reason text;

-- Locations: operator takedown sets status='hidden' (sticky — recompute_confidence preserves
-- 'merged'/'hidden'); these columns record why/when. get_location returns 404 for 'hidden'.
ALTER TABLE locations ADD COLUMN IF NOT EXISTS takedown_reason text;
ALTER TABLE locations ADD COLUMN IF NOT EXISTS takedown_at     timestamptz;

-- 4. Authoritative-source predicate -----------------------------------------
-- TRUE when a location carries any non-'crowd' seed source (salvation_army, goodwill, osm, …).
-- 'crowd' is itself an ingest source (authority_weight 20), so we exclude it by code, not by
-- storage_policy: a purely crowd-submitted pin is NOT authoritative and keeps the low Cold bar.
CREATE OR REPLACE FUNCTION location_is_authoritative(p_location_id bigint)
RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT EXISTS (
    SELECT 1 FROM location_sources ls
    WHERE ls.location_id = p_location_id AND ls.source_code <> 'crowd'
  );
$$;

-- 5. Field-correction consensus: add the seed-source gate + the audit trail ---
-- Reproduces the 0009 body verbatim EXCEPT:
--   * v_req is bumped to >=2 for name/org_name/address on an authoritative location (the gate);
--   * on apply, the prior + new column value(s) and the submitter ip_hash are written to
--     moderation_audit so the change is revertible.
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
  v_actor   text;
  v_applied boolean;
  v_status  correction_status;
  v_eng     integer;
  v_req     integer;
  v_conf    integer;   -- count of confirming OTHER voters (flat 1 each)
  v_reject  integer;   -- count of "no" votes
  v_support integer;   -- submitter(1) + confirmers
  -- prior-value snapshot (for the revert ledger)
  v_old_name   text;
  v_old_otype  text;
  v_old_oname  text;
  v_old_line   text;
  v_old_hn     text;
  v_old_city   text;
  v_old_state  text;
  v_old_postal text;
  v_prior   jsonb;
  v_new     jsonb;
BEGIN
  SELECT location_id, field, proposed_value, proposed_line, proposed_city,
         proposed_state, proposed_postal, submitter_ip_hash, applied, status
    INTO v_loc, v_field, v_val, v_line, v_city, v_state, v_postal, v_actor, v_applied, v_status
  FROM field_corrections WHERE id = p_correction_id;

  IF v_loc IS NULL THEN RETURN; END IF;

  SELECT COALESCE(SUM(CASE WHEN confirm THEN 1 ELSE 0 END), 0),
         COALESCE(SUM(CASE WHEN NOT confirm THEN 1 ELSE 0 END), 0)
    INTO v_conf, v_reject
  FROM field_correction_votes WHERE correction_id = p_correction_id;

  v_support := 1 + v_conf;  -- submitter contributes a flat 1; no GPS weighting for text fields
  v_eng := location_engagement(v_loc);
  v_req := correction_required_support(v_eng);

  -- ABUSE GATE: renaming / re-addressing an authoritatively-sourced location is identity-critical,
  -- so it never auto-applies on a lone good-faith submitter — require at least one independent
  -- confirmer (support>=2) even when Cold. org_type and crowd-only locations are unaffected.
  IF v_field IN ('name', 'org_name', 'address') AND location_is_authoritative(v_loc) THEN
    v_req := GREATEST(v_req, 2);
  END IF;

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

  -- Auto-apply once support reaches the (possibly gated) tier threshold.
  IF v_support >= v_req THEN
    -- Snapshot the affected column(s) BEFORE the change so an operator can revert it.
    SELECT name, org_type::text, org_name, address_line, house_number, city, state, postal_code
      INTO v_old_name, v_old_otype, v_old_oname, v_old_line, v_old_hn, v_old_city, v_old_state, v_old_postal
    FROM locations WHERE id = v_loc;

    IF v_field = 'name' AND v_val IS NOT NULL AND length(btrim(v_val)) > 0 THEN
      UPDATE locations SET name = v_val, updated_at = now() WHERE id = v_loc;
      v_prior := jsonb_build_object('name', v_old_name);
      v_new   := jsonb_build_object('name', v_val);
    ELSIF v_field = 'org_type' AND v_val IS NOT NULL THEN
      UPDATE locations SET org_type = v_val::org_type, updated_at = now() WHERE id = v_loc;
      v_prior := jsonb_build_object('org_type', v_old_otype);
      v_new   := jsonb_build_object('org_type', v_val);
    ELSIF v_field = 'org_name' THEN
      UPDATE locations SET org_name = NULLIF(btrim(v_val), ''), updated_at = now() WHERE id = v_loc;
      v_prior := jsonb_build_object('org_name', v_old_oname);
      v_new   := jsonb_build_object('org_name', NULLIF(btrim(v_val), ''));
    ELSIF v_field = 'address' THEN
      UPDATE locations
         SET address_line = v_line,
             house_number = normalize_house_number(v_line),
             city = v_city, state = v_state, postal_code = v_postal,
             updated_at = now()
       WHERE id = v_loc;
      v_prior := jsonb_build_object('address_line', v_old_line, 'house_number', v_old_hn,
                                    'city', v_old_city, 'state', v_old_state, 'postal_code', v_old_postal);
      v_new   := jsonb_build_object('address_line', v_line,
                                    'house_number', normalize_house_number(v_line),
                                    'city', v_city, 'state', v_state, 'postal_code', v_postal);
    END IF;

    IF v_new IS NOT NULL THEN
      INSERT INTO moderation_audit (location_id, kind, correction_id, field,
                                    prior_value, new_value, actor_ip_hash)
      VALUES (v_loc, 'field_correction', p_correction_id, v_field::text, v_prior, v_new, v_actor);
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

-- 6. Pin-correction consensus: add the audit trail --------------------------
-- Reproduces the 0007 body verbatim (origin-anchored 2 km cap, strict reject rule) EXCEPT that on
-- apply it snapshots the prior position into moderation_audit so a pin move is revertible too.
CREATE OR REPLACE FUNCTION recompute_correction(p_correction_id bigint)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  v_loc     bigint;
  v_lat     double precision;
  v_lon     double precision;
  v_sub_gps boolean;
  v_actor   text;
  v_applied boolean;
  v_status  correction_status;
  v_eng     integer;
  v_req     integer;
  v_conf_w  integer;   -- weighted confirmations from OTHER voters (1 each, 2 if GPS)
  v_reject  integer;   -- count of "no" votes
  v_support integer;   -- submitter weight + confirmer weights
  v_within  boolean;
  v_old_lon double precision;
  v_old_lat double precision;
BEGIN
  SELECT location_id, suggested_lat, suggested_lon, gps_corroborated, submitter_ip_hash, applied, status
    INTO v_loc, v_lat, v_lon, v_sub_gps, v_actor, v_applied, v_status
  FROM location_corrections WHERE id = p_correction_id;

  IF v_loc IS NULL THEN RETURN; END IF;

  SELECT COALESCE(SUM(CASE WHEN confirm THEN 1 + (gps_corroborated)::int ELSE 0 END), 0),
         COALESCE(SUM(CASE WHEN NOT confirm THEN 1 ELSE 0 END), 0)
    INTO v_conf_w, v_reject
  FROM correction_votes WHERE correction_id = p_correction_id;

  -- Submitter contributes weight 1 (2 if they were standing at the spot).
  v_support := (1 + (v_sub_gps)::int) + v_conf_w;

  v_eng := location_engagement(v_loc);
  v_req := correction_required_support(v_eng);

  UPDATE location_corrections
     SET confirmations = v_conf_w, rejections = v_reject,
         support = v_support, required_support = v_req
   WHERE id = p_correction_id;

  -- Already resolved? recompute is idempotent — stop here.
  IF v_applied OR v_status <> 'open' THEN RETURN; END IF;

  -- Reject when clearly out-voted (>=2 rejects and rejects lead the weighted confirms).
  IF v_reject >= 2 AND v_reject > v_conf_w THEN
    UPDATE location_corrections SET status = 'rejected' WHERE id = p_correction_id;
    RETURN;
  END IF;

  -- Auto-apply once support reaches the tier threshold AND the move is within the accuracy cap
  -- measured from the IMMUTABLE origin (not the current geom) so corrections cannot walk a pin.
  IF v_support >= v_req THEN
    SELECT ST_DWithin(COALESCE(l.origin_geom, l.geom)::geography,
                      ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326)::geography, 2000)
      INTO v_within FROM locations l WHERE l.id = v_loc;

    IF COALESCE(v_within, false) THEN
      SELECT ST_X(geom), ST_Y(geom) INTO v_old_lon, v_old_lat FROM locations WHERE id = v_loc;
      UPDATE locations
         SET geom = ST_SetSRID(ST_MakePoint(v_lon, v_lat), 4326), updated_at = now()
       WHERE id = v_loc;
      INSERT INTO moderation_audit (location_id, kind, correction_id, field,
                                    prior_value, new_value, actor_ip_hash)
      VALUES (v_loc, 'pin_correction', p_correction_id, NULL,
              jsonb_build_object('lon', v_old_lon, 'lat', v_old_lat),
              jsonb_build_object('lon', v_lon, 'lat', v_lat), v_actor);
      UPDATE location_corrections
         SET status = 'applied', applied = true, applied_at = now()
       WHERE id = p_correction_id;
      -- Any other open proposals for this location are now moot.
      UPDATE location_corrections
         SET status = 'superseded'
       WHERE location_id = v_loc AND id <> p_correction_id AND status = 'open';
    END IF;
  END IF;
END; $$;

INSERT INTO schema_migrations (version) VALUES ('0010_moderation_audit_and_thresholds.sql') ON CONFLICT DO NOTHING;

COMMIT;
