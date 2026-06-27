from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from .. import db
from ..config import settings
from ..deps import client_ip
from ..imageproc import process_and_save
from ..models import ImageVoteIn
from ..security import ip_hash, token_hash, verify_turnstile

router = APIRouter()


@router.get("/locations/{loc_id}/images")
async def list_images(loc_id: int, include_low: bool = False):
    """Photos for a location. Default: vouched ('visible') only, best first. include_low=true
    also returns 'pending' (new/unverified) and 'hidden' (down-voted) for the gallery toggle."""
    statuses = ["pending", "visible", "hidden"] if include_low else ["visible"]
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id, path, score, upvotes, downvotes, status, applied,
                      (suggested_lat IS NOT NULL) AS is_correction
               FROM location_images
               WHERE location_id = %s AND status = ANY(%s)
               ORDER BY score DESC, created_at DESC""",
            (loc_id, statuses),
        )
        rows = await cur.fetchall()
    return {"images": [
        {"id": r["id"], "url": f"/media/{r['path']}", "score": r["score"],
         "upvotes": r["upvotes"], "downvotes": r["downvotes"], "status": r["status"],
         "is_correction": r["is_correction"], "applied": r["applied"]}
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

    raw = await file.read()
    if len(raw) > settings.image_max_bytes:
        raise HTTPException(413, {"code": "too_large", "message": "image exceeds the size limit"})
    saved = process_and_save(raw, file.content_type or "")
    if saved is None:
        raise HTTPException(422, {"code": "bad_image", "message": "unsupported or invalid image"})
    name, mime = saved

    # both coords or neither
    if (suggested_lat is None) != (suggested_lon is None):
        suggested_lat = suggested_lon = None
    iph = ip_hash(ip)
    thash = token_hash(turnstile_token)

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM location_images "
            "WHERE submitter_ip_hash = %s AND created_at > now() - interval '1 day'",
            (iph,),
        )
        if (await cur.fetchone())["n"] >= settings.image_uploads_per_ip_per_day:
            raise HTTPException(429, {"code": "upload_cooldown", "message": "daily upload limit reached"})

        cur = await conn.execute("SELECT 1 FROM locations WHERE id = %s AND status <> 'merged'", (loc_id,))
        if await cur.fetchone() is None:
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})

        cur = await conn.execute(
            """INSERT INTO location_images
               (location_id, path, mime, submitter_ip_hash, turnstile_hash, suggested_lat, suggested_lon)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (loc_id, name, mime, iph, thash, suggested_lat, suggested_lon),
        )
        img_id = (await cur.fetchone())["id"]

    return {"image_id": img_id, "url": f"/media/{name}", "status": "pending",
            "is_correction": suggested_lat is not None}


@router.post("/images/{img_id}/vote")
async def vote_image(img_id: int, body: ImageVoteIn, request: Request):
    iph = ip_hash(client_ip(request))
    helpful = body.vote == "helpful"
    async with db.pool.connection() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"img{img_id}:{iph}",))
            cur = await conn.execute("SELECT 1 FROM location_images WHERE id = %s", (img_id,))
            if await cur.fetchone() is None:
                raise HTTPException(404, {"code": "not_found", "message": "image not found"})
            await conn.execute(
                """INSERT INTO image_votes (image_id, ip_hash, helpful) VALUES (%s,%s,%s)
                   ON CONFLICT (image_id, ip_hash) DO UPDATE SET helpful = EXCLUDED.helpful, created_at = now()""",
                (img_id, iph, helpful),
            )
        cur = await conn.execute(
            "SELECT score, upvotes, downvotes, status, applied FROM location_images WHERE id = %s", (img_id,)
        )
        r = await cur.fetchone()
    return {"id": img_id, "score": r["score"], "upvotes": r["upvotes"], "downvotes": r["downvotes"],
            "status": r["status"], "applied": r["applied"]}
