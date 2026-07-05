import asyncio
import contextlib
import math
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from .. import db
from ..config import settings
from ..deps import client_ip
from ..imageproc import media_total_bytes, process_and_save, unlink_media
from ..models import ImageVoteIn
from ..security import ip_hash, token_hash, verify_turnstile

router = APIRouter()


@router.get("/locations/{loc_id}/images")
async def list_images(loc_id: int, include_low: bool = False):
    """Photos for a location. Default: vouched ('visible') only, best first. include_low=true
    also returns 'pending' (new/unverified) and 'hidden' (down-voted) for the gallery toggle.
    Operator-removed photos (removed_at set) are NEVER returned by either mode."""
    statuses = ["pending", "visible", "hidden"] if include_low else ["visible"]
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id, path, score, upvotes, downvotes, status, applied, apply_state,
                      (suggested_lat IS NOT NULL) AS is_correction
               FROM location_images
               WHERE location_id = %s AND status = ANY(%s) AND removed_at IS NULL
               ORDER BY score DESC, created_at DESC""",
            (loc_id, statuses),
        )
        rows = await cur.fetchall()
    return {"images": [
        {"id": r["id"], "url": f"/media/{r['path']}", "score": r["score"],
         "upvotes": r["upvotes"], "downvotes": r["downvotes"], "status": r["status"],
         "is_correction": r["is_correction"], "applied": r["applied"],
         "apply_state": r["apply_state"]}
        for r in rows]}


@router.post("/locations/{loc_id}/images")
async def upload_image(
    loc_id: int,
    request: Request,
    file: UploadFile = File(...),
    turnstile_token: str = Form(None),
    suggested_lat: float = Form(None),
    suggested_lon: float = Form(None),
):
    ip = client_ip(request)
    if not await verify_turnstile(turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    # Global media disk ceiling: refuse new photos once the volume is near full, so a flood can't
    # exhaust host storage. Checked before reading/decoding the upload (cheap, cached total).
    if media_total_bytes() >= settings.media_max_total_bytes:
        raise HTTPException(507, {"code": "storage_full",
                                  "message": "photo storage is temporarily full — please try again later"})

    raw = await file.read()
    if len(raw) > settings.image_max_bytes:
        raise HTTPException(413, {"code": "too_large", "message": "image exceeds the size limit"})

    # A photo may propose a corrected pin. Both coords or neither, and — parity with CorrectionIn's
    # bounds and the DB CHECK in migration 0012 — the values must be finite and in range, so NaN/Inf/
    # out-of-range coordinates can never reach ST_MakePoint / locations.geom via the photo path.
    if (suggested_lat is None) != (suggested_lon is None):
        suggested_lat = suggested_lon = None
    if suggested_lat is not None:
        if not (math.isfinite(suggested_lat) and math.isfinite(suggested_lon)
                and -90 <= suggested_lat <= 90 and -180 <= suggested_lon <= 180):
            raise HTTPException(422, {"code": "bad_coords",
                                      "message": "suggested coordinates are out of range"})

    iph = ip_hash(ip)
    thash = token_hash(turnstile_token)

    # Cheap gating BEFORE the expensive decode: the location must exist and the IP must be under its
    # daily cap. This bounds CPU cost (a throttled or bad-target request never decodes) and prevents
    # orphaned files — the decode+save below only runs for a request that will actually be inserted.
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT 1 FROM locations WHERE id = %s AND status <> 'merged'", (loc_id,))
        if await cur.fetchone() is None:
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})
        cur = await conn.execute(
            "SELECT count(*) AS n FROM location_images "
            "WHERE submitter_ip_hash = %s AND created_at > now() - interval '1 day'",
            (iph,),
        )
        if (await cur.fetchone())["n"] >= settings.image_uploads_per_ip_per_day:
            raise HTTPException(429, {"code": "upload_cooldown", "message": "daily upload limit reached"})

    # Decode / downscale / re-encode is CPU-bound (and strips EXIF). Run it in a worker thread so it
    # never blocks the event loop, and hold NO pool connection while it runs. We predetermine the
    # filename and pass it in so cleanup can find the file even if this request is CANCELLED mid-
    # decode (client disconnect / shutdown): the worker thread isn't interruptible and will still
    # write the file, so on any non-commit exit we wait for it to finish, then unlink — no orphan.
    name = f"{uuid.uuid4().hex}.jpg"
    decode = asyncio.ensure_future(asyncio.to_thread(process_and_save, raw, file.content_type or "", name))
    committed = False
    img_id = None
    try:
        saved = await decode
        if saved is None:
            raise HTTPException(422, {"code": "bad_image", "message": "unsupported or invalid image"})
        _name, mime = saved

        # Authoritative write: serialize the per-IP cap with an advisory lock and re-check it inside
        # the transaction (the earlier check is a fast-reject; this is the atomic guarantee against a
        # concurrent burst from one IP).
        async with db.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (iph,))
                cur = await conn.execute(
                    "SELECT count(*) AS n FROM location_images "
                    "WHERE submitter_ip_hash = %s AND created_at > now() - interval '1 day'",
                    (iph,),
                )
                if (await cur.fetchone())["n"] >= settings.image_uploads_per_ip_per_day:
                    raise HTTPException(429, {"code": "upload_cooldown", "message": "daily upload limit reached"})
                cur = await conn.execute(
                    "SELECT 1 FROM locations WHERE id = %s AND status <> 'merged'", (loc_id,))
                if await cur.fetchone() is None:
                    raise HTTPException(404, {"code": "not_found", "message": "location not found"})
                cur = await conn.execute(
                    """INSERT INTO location_images
                       (location_id, path, mime, submitter_ip_hash, turnstile_hash, suggested_lat, suggested_lon)
                       VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (loc_id, name, mime, iph, thash, suggested_lat, suggested_lon),
                )
                img_id = (await cur.fetchone())["id"]
        committed = True
    finally:
        if not committed:
            # Ensure the (uninterruptible) worker thread has finished writing before we unlink, so we
            # never race its write. shield lets it complete even while this coroutine is unwinding.
            with contextlib.suppress(BaseException):
                await asyncio.shield(decode)
            unlink_media(name)

    return {"image_id": img_id, "url": f"/media/{name}", "status": "pending",
            "is_correction": suggested_lat is not None}


@router.post("/images/{img_id}/vote")
async def vote_image(img_id: int, body: ImageVoteIn, request: Request):
    ip = client_ip(request)
    # Image votes auto-apply pin corrections at score >= 3, so gate them behind Turnstile too.
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)
    helpful = body.vote == "helpful"
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"img{img_id}:{iph}",))
            cur = await conn.execute(
                "SELECT submitter_ip_hash FROM location_images WHERE id = %s", (img_id,))
            row = await cur.fetchone()
            if row is None:
                raise HTTPException(404, {"code": "not_found", "message": "image not found"})
            # You can't vouch for your own correction photo — parity with the correction-vote path,
            # which forbids self-votes so the auto-apply threshold reflects independent confirmers.
            if row["submitter_ip_hash"] == iph:
                raise HTTPException(409, {"code": "self_vote", "message": "you can't vote on your own photo"})
            await conn.execute(
                """INSERT INTO image_votes (image_id, ip_hash, helpful, turnstile_hash) VALUES (%s,%s,%s,%s)
                   ON CONFLICT (image_id, ip_hash) DO UPDATE
                     SET helpful = EXCLUDED.helpful, turnstile_hash = EXCLUDED.turnstile_hash, created_at = now()""",
                (img_id, iph, helpful, thash),
            )
        cur = await conn.execute(
            "SELECT score, upvotes, downvotes, status, applied FROM location_images WHERE id = %s", (img_id,)
        )
        r = await cur.fetchone()
    return {"id": img_id, "score": r["score"], "upvotes": r["upvotes"], "downvotes": r["downvotes"],
            "status": r["status"], "applied": r["applied"]}
