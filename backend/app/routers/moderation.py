"""Public content reporting + operator (admin) moderation.

PUBLIC (Turnstile-gated, per-IP rate-limited):
  POST /locations/{id}/report  — file a complaint about a location. NEVER hides the location
                                 (anti-grief: one actor must not be able to pull a seed pin).
  POST /images/{id}/report     — file a complaint about a photo. Once REPORT_IMAGE_HIDE_THRESHOLD
                                 distinct reporters flag the same photo it is soft-hidden
                                 (removed_at set, file KEPT — reversible by an operator).

OPERATOR-ONLY (X-Operator-Token header; every route 404s when OPERATOR_TOKEN is unset or wrong, so
the surface is invisible to probes — see deps.require_operator):
  GET  /admin/reports                     — open report queue (with target summary)
  POST /admin/reports/{id}/resolve        — close a report
  POST /admin/locations/{id}/takedown     — hide a location (status='hidden', sticky)
  POST /admin/locations/{id}/restore      — un-hide a location
  POST /admin/images/{id}/takedown        — remove a photo (removed_at + UNLINK file; permanent)
  POST /admin/images/{id}/restore         — un-hide a photo (clear removed_at; file may be gone)
  GET  /admin/locations/{id}/audit        — moderation_audit rows for a location
  POST /admin/audit/{id}/revert           — revert ONE auto-applied correction
  POST /admin/locations/{id}/revert-all   — revert every un-reverted apply on a location
  POST /admin/revert-actor                — revert every un-reverted apply by one submitter ip_hash

REVERT SEMANTICS: an auto-applied correction is reverted by restoring the column(s) it overwrote
(moderation_audit.prior_value). A row is only restored if its new_value is STILL the live column
value; if a later edit already superseded it, the row is marked reverted as a no-op so it leaves
the active set without clobbering the newer value. Bulk reverts walk newest→oldest per location, so
a chain of edits unwinds cleanly back to the original.
"""
import os.path

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import db
from ..config import settings
from ..deps import client_ip, require_operator
from ..imageproc import unlink_media
from ..models import ReportIn, ResolveReportIn, RevertActorIn, TakedownIn
from ..moderation import screen_text
from ..security import ip_hash, token_hash, verify_turnstile

router = APIRouter()

# Float tolerance when checking whether a pin-correction's applied position is still live.
_GEO_EPS = 1e-7


# --------------------------------------------------------------------------- public reporting

async def _rate_limit_reports(conn, iph: str) -> None:
    cur = await conn.execute(
        "SELECT count(*) AS n FROM content_reports "
        "WHERE reporter_ip_hash = %s AND created_at > now() - interval '1 day'", (iph,))
    if (await cur.fetchone())["n"] >= settings.reports_per_ip_per_day:
        raise HTTPException(429, {"code": "report_cooldown", "message": "daily report limit reached"})


@router.post("/locations/{loc_id}/report")
async def report_location(loc_id: int, body: ReportIn, request: Request):
    """File a complaint about a location. Does NOT change the location's visibility — it lands in
    the operator queue for triage."""
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})
    reason = screen_text(body.reason)
    if reason:
        raise HTTPException(422, {"code": "rejected", "message": reason})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM locations WHERE id = %s AND status <> 'merged'", (loc_id,))
        if await cur.fetchone() is None:
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})
        # Serialize the per-reporter daily cap with the insert (per-IP advisory lock) so a concurrent
        # burst from one reporter can't slip past reports_per_ip_per_day.
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (iph,))
            await _rate_limit_reports(conn, iph)
            await conn.execute(
                """INSERT INTO content_reports (target_type, target_id, reason, reporter_ip_hash, turnstile_hash)
                   VALUES ('location', %s, %s, %s, %s)""",
                (loc_id, (body.reason or "").strip() or None, iph, thash))
    return {"ok": True, "target": "location", "target_id": loc_id, "hidden": False}


@router.post("/images/{img_id}/report")
async def report_image(img_id: int, body: ReportIn, request: Request):
    """File a complaint about a photo. Once enough distinct reporters flag it, the photo is
    soft-hidden (removed_at) so it drops out of every gallery — reversibly (the file is kept)."""
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})
    reason = screen_text(body.reason)
    if reason:
        raise HTTPException(422, {"code": "rejected", "message": reason})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)
    hidden = False
    async with db.pool.connection() as conn:
        async with conn.transaction():
            # Per-reporter advisory lock FIRST (before the image row lock), so the per-reporter daily
            # cap is serialized across reports against DIFFERENT images too, not just this one.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (iph,))
            cur = await conn.execute(
                "SELECT removed_at FROM location_images WHERE id = %s FOR UPDATE", (img_id,))
            img = await cur.fetchone()
            if img is None:
                raise HTTPException(404, {"code": "not_found", "message": "image not found"})
            await _rate_limit_reports(conn, iph)
            await conn.execute(
                """INSERT INTO content_reports (target_type, target_id, reason, reporter_ip_hash, turnstile_hash)
                   VALUES ('image', %s, %s, %s, %s)""",
                (img_id, (body.reason or "").strip() or None, iph, thash))
            # Auto-hide once enough DISTINCT reporters have flagged this photo (and it isn't already
            # removed). Reversible: removed_at is set but the file is NOT unlinked.
            if img["removed_at"] is None:
                cur = await conn.execute(
                    "SELECT count(DISTINCT reporter_ip_hash) AS n FROM content_reports "
                    "WHERE target_type='image' AND target_id=%s AND resolved_at IS NULL", (img_id,))
                if (await cur.fetchone())["n"] >= settings.report_image_hide_threshold:
                    await conn.execute(
                        "UPDATE location_images SET removed_at = now(), "
                        "removed_reason = %s WHERE id = %s AND removed_at IS NULL",
                        (f"auto-hidden: {settings.report_image_hide_threshold}+ community reports", img_id))
                    hidden = True
    return {"ok": True, "target": "image", "target_id": img_id, "hidden": hidden}


# --------------------------------------------------------------------------- operator: reports

@router.get("/admin/reports", dependencies=[Depends(require_operator)])
async def list_reports(limit: int = 200):
    """Open (unresolved) reports, newest first, with a small target summary for triage."""
    limit = max(1, min(limit, 1000))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT r.id, r.target_type, r.target_id, r.reason, r.reporter_ip_hash, r.created_at,
                      l.name AS loc_name, l.status AS loc_status,
                      i.location_id AS img_location_id, i.status AS img_status,
                      (i.removed_at IS NOT NULL) AS img_removed
               FROM content_reports r
               LEFT JOIN locations l       ON r.target_type='location' AND l.id = r.target_id
               LEFT JOIN location_images i ON r.target_type='image'    AND i.id = r.target_id
               WHERE r.resolved_at IS NULL
               ORDER BY r.created_at DESC
               LIMIT %s""",
            (limit,))
        rows = await cur.fetchall()
    return {"reports": [dict(r) for r in rows], "count": len(rows)}


@router.post("/admin/reports/{report_id}/resolve", dependencies=[Depends(require_operator)])
async def resolve_report(report_id: int, body: ResolveReportIn):
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE content_reports SET resolved_at = now(), resolved_note = %s "
            "WHERE id = %s AND resolved_at IS NULL RETURNING id",
            ((body.note or "").strip() or None, report_id))
        ok = await cur.fetchone() is not None
    if not ok:
        raise HTTPException(404, {"code": "not_found", "message": "open report not found"})
    return {"ok": True, "report_id": report_id}


async def _resolve_open_reports(conn, target_type: str, target_id: int, note: str) -> None:
    await conn.execute(
        "UPDATE content_reports SET resolved_at = now(), resolved_note = %s "
        "WHERE target_type = %s AND target_id = %s AND resolved_at IS NULL",
        (note, target_type, target_id))


# --------------------------------------------------------------------------- operator: takedown

@router.post("/admin/locations/{loc_id}/takedown", dependencies=[Depends(require_operator)])
async def takedown_location(loc_id: int, body: TakedownIn):
    """Hide a location from the public site. status='hidden' is sticky (recompute_confidence
    preserves it), so it stays down regardless of later votes."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "UPDATE locations SET status='hidden', takedown_reason=%s, takedown_at=now() "
                "WHERE id=%s AND status <> 'merged' RETURNING id",
                ((body.reason or "").strip() or None, loc_id))
            if await cur.fetchone() is None:
                raise HTTPException(404, {"code": "not_found", "message": "location not found"})
            await _resolve_open_reports(conn, "location", loc_id, "location taken down")
    return {"ok": True, "location_id": loc_id, "status": "hidden"}


@router.post("/admin/locations/{loc_id}/restore", dependencies=[Depends(require_operator)])
async def restore_location(loc_id: int):
    """Un-hide a taken-down location. Restores active/pending from confidence (mirrors the
    recompute_confidence gate at 25), and clears the takedown record."""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE locations "
            "SET status = CASE WHEN confidence >= 25 THEN 'active' ELSE 'pending' END::location_status, "
            "    takedown_reason = NULL, takedown_at = NULL "
            "WHERE id = %s AND status = 'hidden' RETURNING status",
            (loc_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, {"code": "not_found", "message": "hidden location not found"})
    return {"ok": True, "location_id": loc_id, "status": row["status"]}


@router.post("/admin/images/{img_id}/takedown", dependencies=[Depends(require_operator)])
async def takedown_image(img_id: int, body: TakedownIn):
    """Permanently remove a photo: set removed_at (filters it from every gallery) and UNLINK the
    file from the media volume (so the raw /media/<path> 404s too). The row is kept as the record."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT path FROM location_images WHERE id = %s", (img_id,))
            img = await cur.fetchone()
            if img is None:
                raise HTTPException(404, {"code": "not_found", "message": "image not found"})
            await conn.execute(
                "UPDATE location_images SET removed_at = COALESCE(removed_at, now()), removed_reason = %s "
                "WHERE id = %s",
                ((body.reason or "").strip() or "operator takedown", img_id))
            await _resolve_open_reports(conn, "image", img_id, "photo removed")
        file_removed = unlink_media(img["path"])
    return {"ok": True, "image_id": img_id, "file_removed": file_removed}


@router.post("/admin/images/{img_id}/restore", dependencies=[Depends(require_operator)])
async def restore_image(img_id: int):
    """Un-hide a soft-hidden photo by clearing removed_at. If the file was already unlinked by a
    hard takedown, the row reappears but the image will 404 (file_present=false signals this)."""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE location_images SET removed_at = NULL, removed_reason = NULL "
            "WHERE id = %s AND removed_at IS NOT NULL RETURNING path", (img_id,))
        row = await cur.fetchone()
    if row is None:
        raise HTTPException(404, {"code": "not_found", "message": "hidden image not found"})
    file_present = os.path.isfile(os.path.join(settings.media_dir, row["path"]))
    return {"ok": True, "image_id": img_id, "file_present": file_present}


# --------------------------------------------------------------------------- operator: revert

async def _revert_one(conn, row, note: str | None) -> str:
    """Revert one moderation_audit row. Returns 'reverted', 'superseded', or 'already'.

    Restores prior_value only if new_value is still the live column value; otherwise the row is
    marked reverted as a no-op (a later edit already replaced it) so it leaves the active set."""
    if row["reverted_at"] is not None:
        return "already"
    loc, kind, field = row["location_id"], row["kind"], row["field"]
    prior, new = row["prior_value"], row["new_value"]

    # Is this apply still the live value? (Guards against clobbering a newer legitimate edit.)
    if kind == "pin_correction":
        cur = await conn.execute("SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat FROM locations WHERE id=%s", (loc,))
        c = await cur.fetchone()
        live = c is not None and abs(c["lon"] - new["lon"]) < _GEO_EPS and abs(c["lat"] - new["lat"]) < _GEO_EPS
    else:
        cur = await conn.execute(
            "SELECT name, org_type::text AS org_type, org_name, address_line, house_number, "
            "city, state, postal_code FROM locations WHERE id=%s", (loc,))
        c = await cur.fetchone()
        live = c is not None and all(c.get(k) == v for k, v in new.items())

    if not live:
        await conn.execute(
            "UPDATE moderation_audit SET reverted_at = now(), reverted_note = %s WHERE id = %s",
            (f"superseded (no-op); {note}" if note else "superseded (no-op)", row["id"]))
        return "superseded"

    # Restore the prior column value(s).
    if kind == "pin_correction":
        await conn.execute(
            "UPDATE locations SET geom = ST_SetSRID(ST_MakePoint(%s,%s),4326), updated_at=now() WHERE id=%s",
            (prior["lon"], prior["lat"], loc))
    elif field == "name":
        await conn.execute("UPDATE locations SET name=%s, updated_at=now() WHERE id=%s", (prior.get("name"), loc))
    elif field == "org_type":
        await conn.execute("UPDATE locations SET org_type=%s::org_type, updated_at=now() WHERE id=%s",
                           (prior.get("org_type"), loc))
    elif field == "org_name":
        await conn.execute("UPDATE locations SET org_name=%s, updated_at=now() WHERE id=%s",
                           (prior.get("org_name"), loc))
    elif field == "address":
        await conn.execute(
            "UPDATE locations SET address_line=%s, house_number=%s, city=%s, state=%s, postal_code=%s, "
            "updated_at=now() WHERE id=%s",
            (prior.get("address_line"), prior.get("house_number"), prior.get("city"),
             prior.get("state"), prior.get("postal_code"), loc))
    else:
        return "superseded"  # unknown shape — leave columns, but don't loop on it
    await conn.execute(
        "UPDATE moderation_audit SET reverted_at = now(), reverted_note = %s WHERE id = %s",
        (note, row["id"]))
    return "reverted"


@router.get("/admin/locations/{loc_id}/audit", dependencies=[Depends(require_operator)])
async def location_audit(loc_id: int, limit: int = 200):
    limit = max(1, min(limit, 1000))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id, kind, correction_id, field, prior_value, new_value, actor_ip_hash,
                      applied_at, reverted_at, reverted_note
               FROM moderation_audit WHERE location_id = %s
               ORDER BY applied_at DESC LIMIT %s""",
            (loc_id, limit))
        rows = await cur.fetchall()
    return {"audit": [dict(r) for r in rows], "count": len(rows)}


@router.post("/admin/audit/{audit_id}/revert", dependencies=[Depends(require_operator)])
async def revert_audit(audit_id: int, body: ResolveReportIn):
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, location_id, kind, field, prior_value, new_value, reverted_at "
                "FROM moderation_audit WHERE id = %s FOR UPDATE", (audit_id,))
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(404, {"code": "not_found", "message": "audit entry not found"})
            if row["reverted_at"] is not None:
                raise HTTPException(409, {"code": "already_reverted", "message": "already reverted"})
            result = await _revert_one(conn, row, (body.note or "").strip() or None)
    return {"ok": True, "audit_id": audit_id, "result": result}


@router.post("/admin/locations/{loc_id}/revert-all", dependencies=[Depends(require_operator)])
async def revert_all(loc_id: int, body: ResolveReportIn):
    """Revert every un-reverted auto-apply on a location, newest→oldest, unwinding the edit chain
    back to the original column values."""
    note = (body.note or "").strip() or None
    reverted = superseded = 0
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, location_id, kind, field, prior_value, new_value, reverted_at "
                "FROM moderation_audit WHERE location_id = %s AND reverted_at IS NULL "
                "ORDER BY applied_at DESC FOR UPDATE", (loc_id,))
            for row in await cur.fetchall():
                r = await _revert_one(conn, row, note)
                if r == "reverted":
                    reverted += 1
                elif r == "superseded":
                    superseded += 1
    return {"ok": True, "location_id": loc_id, "reverted": reverted, "superseded": superseded}


@router.post("/admin/revert-actor", dependencies=[Depends(require_operator)])
async def revert_actor(body: RevertActorIn):
    """Revert every un-reverted auto-apply authored by one submitter ip_hash, across all locations.
    Processed per-location newest→oldest so chains unwind correctly; applies already superseded by
    a later edit are marked no-op rather than clobbering the newer value."""
    note = (body.note or "").strip() or None
    reverted = superseded = 0
    locations: set[int] = set()
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute(
                "SELECT id, location_id, kind, field, prior_value, new_value, reverted_at "
                "FROM moderation_audit WHERE actor_ip_hash = %s AND reverted_at IS NULL "
                "ORDER BY location_id, applied_at DESC FOR UPDATE", (body.actor_ip_hash,))
            for row in await cur.fetchall():
                r = await _revert_one(conn, row, note)
                locations.add(row["location_id"])
                if r == "reverted":
                    reverted += 1
                elif r == "superseded":
                    superseded += 1
    return {"ok": True, "actor_ip_hash": body.actor_ip_hash, "reverted": reverted,
            "superseded": superseded, "locations_affected": len(locations)}


# --------------------------------------------------------------------------- operator: photo-move queue

@router.get("/admin/images/pending-moves", dependencies=[Depends(require_operator)])
async def list_pending_image_moves(limit: int = 200):
    """Photo pin-moves held for operator review (Band B: >250 m and <=2 km from origin, with enough
    independent support). The pin has NOT been moved — apply-move commits it, reject-move drops it.
    Distance is the actual move (metres from the immutable origin); independent_voters excludes the
    photo's own submitter. WHERE apply_state='pending_review' matches the partial-index predicate."""
    limit = max(1, min(limit, 1000))
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT i.id AS image_id, i.location_id, i.score,
                      ST_Distance(COALESCE(l.origin_geom, l.geom)::geography,
                                  ST_SetSRID(ST_MakePoint(i.suggested_lon, i.suggested_lat), 4326)::geography)
                        AS distance_m,
                      (SELECT count(DISTINCT iv.ip_hash)
                         FILTER (WHERE iv.helpful AND iv.ip_hash <> i.submitter_ip_hash)
                       FROM image_votes iv WHERE iv.image_id = i.id) AS independent_voters,
                      i.created_at
               FROM location_images i
               JOIN locations l ON l.id = i.location_id
               WHERE i.apply_state = 'pending_review'
               ORDER BY i.created_at ASC
               LIMIT %s""",
            (limit,))
        rows = await cur.fetchall()
    return {"pending_moves": [dict(r) for r in rows], "count": len(rows)}


@router.post("/admin/images/{img_id}/apply-move", dependencies=[Depends(require_operator)])
async def apply_image_move(img_id: int):
    """Commit a held (pending_review) photo pin-move. Re-checks the 2 km origin cap inside the DB
    function, moves the pin, and writes the same revertible moderation_audit row a Band A auto-apply
    would have. Surfaces the function result: 'applied' | 'too_far' (409) | 'not_pending' (409)."""
    async with db.pool.connection() as conn:
        async with conn.transaction():
            cur = await conn.execute("SELECT apply_pending_image_move(%s) AS result", (img_id,))
            result = (await cur.fetchone())["result"]
    if result == "applied":
        return {"ok": True, "image_id": img_id, "result": result}
    if result == "too_far":
        raise HTTPException(409, {"code": "too_far",
                                  "message": "suggested move exceeds the 2 km origin cap"})
    # 'not_pending' — no pending-review move on this image (never queued, or already resolved).
    raise HTTPException(409, {"code": "not_pending", "message": "no pending-review move for this image"})


@router.post("/admin/images/{img_id}/reject-move", dependencies=[Depends(require_operator)])
async def reject_image_move(img_id: int):
    """Reject a held photo pin-move: set apply_state='rejected'. NEVER moves the pin. Idempotent —
    only a row currently in pending_review transitions (404 otherwise)."""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "UPDATE location_images SET apply_state = 'rejected' "
            "WHERE id = %s AND apply_state = 'pending_review' RETURNING id", (img_id,))
        ok = await cur.fetchone() is not None
    if not ok:
        raise HTTPException(404, {"code": "not_found", "message": "no pending-review move for this image"})
    return {"ok": True, "image_id": img_id, "apply_state": "rejected"}
