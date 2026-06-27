from fastapi import APIRouter, HTTPException, Request

from .. import db
from ..config import bucket
from ..deps import client_ip
from ..models import VoteIn
from ..security import ip_hash, token_hash, verify_turnstile

router = APIRouter()


@router.post("/locations/{loc_id}/vote")
async def vote(loc_id: int, body: VoteIn, request: Request):
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)

    async with db.pool.connection() as conn:
        async with conn.transaction():
            # Serialize concurrent votes for the same voter+location (race-safe cooldown).
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{loc_id}:{iph}",))

            cur = await conn.execute("SELECT status FROM locations WHERE id = %s", (loc_id,))
            row = await cur.fetchone()
            if row is None or row["status"] == "merged":
                raise HTTPException(404, {"code": "not_found", "message": "location not found"})

            cur = await conn.execute(
                """SELECT created_at FROM votes
                   WHERE location_id = %s AND ip_hash = %s AND created_at > now() - interval '24 hours'
                   ORDER BY created_at DESC LIMIT 1""",
                (loc_id, iph),
            )
            recent = await cur.fetchone()
            if recent is not None:
                cur = await conn.execute(
                    "SELECT GREATEST(0, EXTRACT(EPOCH FROM (%s + interval '24 hours' - now()))::int) AS ra",
                    (recent["created_at"],),
                )
                retry_after = (await cur.fetchone())["ra"]
                raise HTTPException(
                    429,
                    {"code": "cooldown_active", "message": "already voted on this location in the last 24h",
                     "retry_after": retry_after},
                )

            await conn.execute(
                "INSERT INTO votes (location_id, vote, ip_hash, turnstile_hash) VALUES (%s, %s, %s, %s)",
                (loc_id, body.vote, iph, thash),
            )
        # Transaction committed; trigger has recomputed confidence/status.
        cur = await conn.execute(
            "SELECT confidence, status, upvotes, denies FROM locations WHERE id = %s", (loc_id,)
        )
        r = await cur.fetchone()

    conf = float(r["confidence"])
    return {"id": loc_id, "confidence": conf, "bucket": bucket(conf),
            "status": r["status"], "upvotes": r["upvotes"], "denies": r["denies"]}
