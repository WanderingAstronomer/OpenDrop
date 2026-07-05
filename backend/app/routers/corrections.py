"""Photo-optional pin corrections + community attribute ratings.

The hard consensus logic lives in the DB (recompute_correction in migration 0006). These
endpoints validate, rate-limit, Turnstile-gate, and write rows; the triggers do the math and
may auto-apply a move. We only ever read back the resulting state.

GPS privacy contract: `gps_corroborated` is a boolean the CLIENT computes from the device's own
location ("am I within the radius of the suggested point?"). The server never receives, stores,
correlates, or transmits coordinates from a user's device. GPS only ADDS consensus weight.
"""
from typing import get_args

from fastapi import APIRouter, HTTPException, Request

from .. import db
from ..community import ATTRIBUTE_MAX, attribute_aggregates
from ..config import settings
from ..deps import client_ip
from ..models import (
    AttributeClearIn, AttributeIn, CorrectionIn, CorrectionVoteIn,
    FieldCorrectionIn, FieldCorrectionVoteIn, OrgType,
)
from ..moderation import screen_submission, screen_text
from ..security import ip_hash, token_hash, verify_turnstile

router = APIRouter()

_VALID_ORG_TYPES = frozenset(get_args(OrgType))


@router.post("/locations/{loc_id}/corrections")
async def propose_correction(loc_id: int, body: CorrectionIn, request: Request):
    """Propose a corrected pin location. On a low-engagement (Cold) location this auto-applies
    immediately on good faith; busier locations need confirmations before the pin moves."""
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    note_reason = screen_text(body.note)
    if note_reason:
        raise HTTPException(422, {"code": "rejected", "message": note_reason})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, ST_X(COALESCE(origin_geom, geom)) AS olon, "
            "ST_Y(COALESCE(origin_geom, geom)) AS olat FROM locations WHERE id = %s", (loc_id,))
        loc = await cur.fetchone()
        if loc is None or loc["status"] == "merged":
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})

        # Distance guard: a correction fixes accuracy, it does not relocate a business across town.
        # Measured from the IMMUTABLE origin (matching recompute_correction's cap in migration 0007)
        # so a sequence of small legal moves can't walk a pin across the map.
        cur = await conn.execute(
            """SELECT ST_Distance(ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,
                                  ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography) AS d""",
            (loc["olon"], loc["olat"], body.suggested_lon, body.suggested_lat))
        dist = (await cur.fetchone())["d"]
        if dist is not None and dist > settings.correction_max_move_m:
            raise HTTPException(422, {
                "code": "move_too_far",
                "message": (f"a correction can move a pin at most {settings.correction_max_move_m} m — "
                            "for a larger move, add a new location or report this one as gone"),
                "details": {"distance_m": round(dist), "max_m": settings.correction_max_move_m}})

        if body.image_id is not None:
            cur = await conn.execute(
                "SELECT 1 FROM location_images WHERE id = %s AND location_id = %s", (body.image_id, loc_id))
            if await cur.fetchone() is None:
                raise HTTPException(422, {"code": "bad_image", "message": "image does not belong to this location"})

        # Serialize the per-IP daily cap with the insert: the count+insert run under a per-IP advisory
        # lock inside one transaction, so a concurrent burst from one IP can't all read n<cap and each
        # slip a row past the limit (the check-then-act race the vote endpoint already guards against).
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (iph,))
            cur = await conn.execute(
                "SELECT count(*) AS n FROM location_corrections "
                "WHERE submitter_ip_hash = %s AND created_at > now() - interval '1 day'", (iph,))
            if (await cur.fetchone())["n"] >= settings.corrections_per_ip_per_day:
                raise HTTPException(429, {"code": "correction_cooldown", "message": "daily correction limit reached"})
            # Insert fires the after-insert trigger, which may auto-apply (Cold good-faith).
            cur = await conn.execute(
                """INSERT INTO location_corrections
                   (location_id, suggested_lat, suggested_lon, note, image_id,
                    submitter_ip_hash, turnstile_hash, gps_corroborated)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (loc_id, body.suggested_lat, body.suggested_lon, body.note, body.image_id,
                 iph, thash, body.gps_corroborated))
            corr_id = (await cur.fetchone())["id"]
        c = await _correction_state(conn, corr_id)

    return {"correction_id": corr_id, **c}


@router.post("/corrections/{corr_id}/vote")
async def vote_correction(corr_id: int, body: CorrectionVoteIn, request: Request):
    """Confirm or reject a pending correction. Reaching the engagement-tier threshold of weighted
    support auto-applies the move. You cannot confirm your own proposal."""
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)

    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"corr{corr_id}:{iph}",))
            cur = await conn.execute(
                "SELECT submitter_ip_hash, status FROM location_corrections WHERE id = %s", (corr_id,))
            corr = await cur.fetchone()
            if corr is None:
                raise HTTPException(404, {"code": "not_found", "message": "correction not found"})
            if corr["submitter_ip_hash"] == iph:
                raise HTTPException(409, {"code": "self_vote", "message": "you can't vote on your own correction"})
            if corr["status"] != "open":
                raise HTTPException(409, {"code": "correction_closed",
                                          "message": f"correction already {corr['status']}",
                                          "details": {"status": corr["status"]}})
            await conn.execute(
                """INSERT INTO correction_votes (correction_id, ip_hash, confirm, gps_corroborated, turnstile_hash)
                   VALUES (%s,%s,%s,%s,%s)
                   ON CONFLICT (correction_id, ip_hash) DO UPDATE
                     SET confirm = EXCLUDED.confirm, gps_corroborated = EXCLUDED.gps_corroborated,
                         turnstile_hash = EXCLUDED.turnstile_hash, created_at = now()""",
                (corr_id, iph, body.confirm, body.gps_corroborated, thash))
        # Trigger has recomputed; read the resulting state outside the txn.
        c = await _correction_state(conn, corr_id)

    return {"correction_id": corr_id, **c}


@router.post("/locations/{loc_id}/attributes")
async def rate_attribute(loc_id: int, body: AttributeIn, request: Request):
    """Rate perceived safety / bin condition / number of bins. One rating per attribute per
    person; re-rating overwrites. Accepts a single {attribute, value} (legacy) or a batched
    {ratings:[{attribute, value|null}, ...]} — the rate-form's single Save submits all touched
    ratings in ONE call/token instead of one call per tap; a null value retracts the caller's own
    rating for that attribute. Returns the updated aggregates."""
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    # Normalize both accepted shapes into one list of (attribute, value-or-None) operations.
    if body.ratings is not None:
        ops = [(r.attribute, r.value) for r in body.ratings]
    elif body.attribute is not None and body.value is not None:
        ops = [(body.attribute, body.value)]
    else:
        raise HTTPException(422, {"code": "bad_request",
                                  "message": "provide attribute+value, or ratings=[...]"})
    if not ops:
        raise HTTPException(422, {"code": "bad_request", "message": "ratings is empty"})
    seen: set[str] = set()
    for attr, val in ops:
        if attr in seen:
            raise HTTPException(422, {"code": "bad_request", "message": f"duplicate attribute {attr}"})
        seen.add(attr)
        if val is not None and val > ATTRIBUTE_MAX[attr]:
            raise HTTPException(422, {"code": "bad_value",
                                      "message": f"{attr} value must be 1..{ATTRIBUTE_MAX[attr]}"})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)
    sets = [(a, v) for a, v in ops if v is not None]
    clears = [a for a, v in ops if v is None]

    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM locations WHERE id = %s AND status <> 'merged'", (loc_id,))
        if await cur.fetchone() is None:
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})

        # Per-IP-per-day cap, parity with every other community write path. Only NEW
        # (location, attribute) pairs count — refining a rating you already left is an UPDATE and
        # is always allowed, so the cap bounds how many distinct spots one IP can rate, never how
        # often they correct a single rating. A batch counts each new pair it would create.
        # The count and the writes run under one per-IP advisory lock so a concurrent burst can't
        # slip extra new pairs past the cap (the check-then-act race).
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (iph,))
            new_pairs = 0
            for attr, _ in sets:
                cur = await conn.execute(
                    "SELECT 1 FROM attribute_votes WHERE location_id = %s AND ip_hash = %s AND attribute = %s",
                    (loc_id, iph, attr))
                if await cur.fetchone() is None:
                    new_pairs += 1
            if new_pairs:
                cur = await conn.execute(
                    "SELECT count(*) AS n FROM attribute_votes "
                    "WHERE ip_hash = %s AND updated_at > now() - interval '1 day'", (iph,))
                if (await cur.fetchone())["n"] + new_pairs > settings.attributes_per_ip_per_day:
                    raise HTTPException(429, {"code": "attribute_cooldown", "message": "daily rating limit reached"})

            for attr, val in sets:
                await conn.execute(
                    """INSERT INTO attribute_votes (location_id, ip_hash, attribute, value, turnstile_hash)
                       VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (location_id, ip_hash, attribute) DO UPDATE
                         SET value = EXCLUDED.value, turnstile_hash = EXCLUDED.turnstile_hash, updated_at = now()""",
                    (loc_id, iph, attr, val, thash))
            for attr in clears:
                await conn.execute(
                    "DELETE FROM attribute_votes WHERE location_id = %s AND ip_hash = %s AND attribute = %s",
                    (loc_id, iph, attr))
        attributes = await attribute_aggregates(conn, loc_id)

    return {"id": loc_id, "attributes": attributes}


@router.delete("/locations/{loc_id}/attributes/{attribute}")
async def clear_attribute(loc_id: int, attribute: str, body: AttributeClearIn, request: Request):
    """Retract the caller's own rating for one attribute (rating deselect). Idempotent — clearing a
    rating you never left is a no-op. Returns the recomputed aggregates so the UI can re-render."""
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})
    if attribute not in ATTRIBUTE_MAX:
        raise HTTPException(422, {"code": "bad_attribute", "message": "unknown attribute"})

    iph = ip_hash(ip)

    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM locations WHERE id = %s AND status <> 'merged'", (loc_id,))
        if await cur.fetchone() is None:
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})

        await conn.execute(
            "DELETE FROM attribute_votes WHERE location_id = %s AND ip_hash = %s AND attribute = %s",
            (loc_id, iph, attribute))
        attributes = await attribute_aggregates(conn, loc_id)

    return {"id": loc_id, "attributes": attributes}


@router.post("/locations/{loc_id}/field-corrections")
async def propose_field_correction(loc_id: int, body: FieldCorrectionIn, request: Request):
    """Propose a better name / type / owning org / address for a location. Same engagement-tiered
    consensus as a pin correction: a Cold location auto-applies on good faith, busier ones need
    confirmations. Text fields carry no GPS weight, so every voice is a flat 1."""
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    note_reason = screen_text(body.note)
    if note_reason:
        raise HTTPException(422, {"code": "rejected", "message": note_reason})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)

    # Normalize + validate the proposal, and capture the value(s) to store, per field.
    field = body.field
    pv = None                       # proposed_value (scalar fields)
    line = city = state = postal = None  # address fields
    if field == "name":
        pv = (body.value or "").strip()
        reason = screen_submission(pv)
        if reason:
            raise HTTPException(422, {"code": "rejected", "message": reason})
    elif field == "org_type":
        pv = (body.value or "").strip()
        if pv not in _VALID_ORG_TYPES:
            raise HTTPException(422, {"code": "bad_value", "message": "unknown organization type"})
    elif field == "org_name":
        pv = (body.value or "").strip()
        reason = screen_submission(pv)
        if reason:
            raise HTTPException(422, {"code": "rejected", "message": reason})
    else:  # address
        a = body.address
        if a is None or not (a.line or "").strip():
            raise HTTPException(422, {"code": "bad_value", "message": "a street line is required"})
        line = (a.line or "").strip()
        city = (a.city or "").strip() or None
        state = ((a.state or "").strip().upper() or None)
        postal = (a.postal_code or "").strip() or None
        if state is not None and len(state) != 2:
            raise HTTPException(422, {"code": "bad_value", "message": "state must be a 2-letter code"})
        reason = screen_submission(line, city, postal)
        if reason:
            raise HTTPException(422, {"code": "rejected", "message": reason})

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT status, name, org_type, org_name, address_line, city, state, postal_code "
            "FROM locations WHERE id = %s", (loc_id,))
        loc = await cur.fetchone()
        if loc is None or loc["status"] == "merged":
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})

        # Don't let people propose the value that's already there.
        nochange = (
            (field == "name" and pv == (loc["name"] or "").strip())
            or (field == "org_type" and pv == loc["org_type"])
            or (field == "org_name" and pv == (loc["org_name"] or "").strip())
            or (field == "address"
                and line == (loc["address_line"] or "").strip()
                and city == ((loc["city"] or "").strip() or None)
                and state == ((loc["state"] or "").strip().upper() or None)
                and postal == ((loc["postal_code"] or "").strip() or None))
        )
        if nochange:
            raise HTTPException(422, {"code": "no_change", "message": "that's already the current value"})

        # Serialize the duplicate-proposal check, the per-IP daily cap, and the insert under one
        # per-IP advisory lock, so concurrent requests from one IP can't race past either guard
        # (the partial unique index field_corrections_one_open is the DB backstop for the former).
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (iph,))
            # One open proposal per (location, field) per person.
            cur = await conn.execute(
                "SELECT 1 FROM field_corrections WHERE location_id = %s AND field = %s "
                "AND submitter_ip_hash = %s AND status = 'open' LIMIT 1", (loc_id, field, iph))
            if await cur.fetchone() is not None:
                raise HTTPException(409, {"code": "duplicate_proposal",
                                          "message": "you already have an open proposal for this field"})

            # Per-IP-per-day cap, parity with pin corrections.
            cur = await conn.execute(
                "SELECT count(*) AS n FROM field_corrections "
                "WHERE submitter_ip_hash = %s AND created_at > now() - interval '1 day'", (iph,))
            if (await cur.fetchone())["n"] >= settings.corrections_per_ip_per_day:
                raise HTTPException(429, {"code": "correction_cooldown", "message": "daily correction limit reached"})

            # Insert fires the after-insert trigger, which may auto-apply (Cold good-faith).
            cur = await conn.execute(
                """INSERT INTO field_corrections
                   (location_id, field, proposed_value, proposed_line, proposed_city,
                    proposed_state, proposed_postal, note, submitter_ip_hash, turnstile_hash)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (loc_id, field, pv, line, city, state, postal, body.note, iph, thash))
            corr_id = (await cur.fetchone())["id"]
        c = await _field_correction_state(conn, corr_id)

    return {"correction_id": corr_id, **c}


@router.post("/field-corrections/{corr_id}/vote")
async def vote_field_correction(corr_id: int, body: FieldCorrectionVoteIn, request: Request):
    """Confirm or reject an open field correction. Reaching the engagement-tier support threshold
    auto-applies the change. You cannot vote on your own proposal."""
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)

    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"fcorr{corr_id}:{iph}",))
            cur = await conn.execute(
                "SELECT submitter_ip_hash, status FROM field_corrections WHERE id = %s", (corr_id,))
            corr = await cur.fetchone()
            if corr is None:
                raise HTTPException(404, {"code": "not_found", "message": "correction not found"})
            if corr["submitter_ip_hash"] == iph:
                raise HTTPException(409, {"code": "self_vote", "message": "you can't vote on your own correction"})
            if corr["status"] != "open":
                raise HTTPException(409, {"code": "correction_closed",
                                          "message": f"correction already {corr['status']}",
                                          "details": {"status": corr["status"]}})
            await conn.execute(
                """INSERT INTO field_correction_votes (correction_id, ip_hash, confirm, turnstile_hash)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (correction_id, ip_hash) DO UPDATE
                     SET confirm = EXCLUDED.confirm, turnstile_hash = EXCLUDED.turnstile_hash,
                         created_at = now()""",
                (corr_id, iph, body.confirm, thash))
        # Trigger has recomputed; read the resulting state outside the txn.
        c = await _field_correction_state(conn, corr_id)

    return {"correction_id": corr_id, **c}


async def _field_correction_state(conn, corr_id: int) -> dict:
    cur = await conn.execute(
        "SELECT status, applied, support, required_support, confirmations, rejections "
        "FROM field_corrections WHERE id = %s", (corr_id,))
    c = await cur.fetchone()
    return {"status": c["status"], "applied": c["applied"], "support": c["support"],
            "required_support": c["required_support"], "confirmations": c["confirmations"],
            "rejections": c["rejections"]}


async def _correction_state(conn, corr_id: int) -> dict:
    cur = await conn.execute(
        "SELECT status, applied, support, required_support, confirmations, rejections "
        "FROM location_corrections WHERE id = %s", (corr_id,))
    c = await cur.fetchone()
    return {"status": c["status"], "applied": c["applied"], "support": c["support"],
            "required_support": c["required_support"], "confirmations": c["confirmations"],
            "rejections": c["rejections"]}
