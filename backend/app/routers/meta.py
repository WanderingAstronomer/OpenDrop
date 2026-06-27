from fastapi import APIRouter
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .. import db
from ..config import BUCKETS, settings

router = APIRouter()


@router.get("/health")
async def health():
    ok = False
    try:
        async with db.pool.connection() as conn:
            await conn.execute("SELECT 1")
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    return {"status": "ok" if ok else "degraded", "db": ok}


@router.get("/meta")
async def meta():
    async with db.pool.connection() as conn:
        cur = await conn.execute("SELECT status, count(*) AS n FROM locations GROUP BY status")
        counts = {r["status"]: r["n"] for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT org_type, count(*) AS n FROM locations WHERE status='active' GROUP BY org_type"
        )
        by_type = {r["org_type"]: r["n"] for r in await cur.fetchall()}
        # Only ingest sources that actually contribute >=1 active location (no enrich_only, no empties).
        cur = await conn.execute(
            """SELECT s.code, s.display_name, s.attribution, s.license
               FROM sources s
               WHERE s.storage_policy = 'ingest'
                 AND EXISTS (SELECT 1 FROM location_sources ls JOIN locations l ON l.id = ls.location_id
                             WHERE ls.source_code = s.code AND l.status = 'active')
               ORDER BY s.code"""
        )
        sources = [dict(r) for r in await cur.fetchall()]
    return {
        "counts": {"active": counts.get("active", 0), "pending": counts.get("pending", 0), "by_type": by_type},
        "sources": sources,
        "turnstile_sitekey": settings.turnstile_sitekey,
        "confidence_buckets": BUCKETS,
    }


@router.get("/export")
async def export(state: str | None = None):
    """Redistributable open-data dump. Reads v_public_locations only (active + redistributable),
    with ODbL/per-source attribution embedded IN the payload (survives a saved file)."""
    where = ""
    params: list = []
    if state:
        where = "WHERE state = %s"
        params.append(state.upper())
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            f"""SELECT id, ST_X(geom) AS lon, ST_Y(geom) AS lat, name, org_type, org_name,
                       address_line, city, state, postal_code, hours, accepted_items, phone, website,
                       confidence, last_verified_at
                FROM v_public_locations {where}""",
            params,
        )
        rows = await cur.fetchall()
        cur = await conn.execute(
            """SELECT DISTINCT s.attribution, s.license FROM sources s
               WHERE s.storage_policy = 'ingest'
                 AND EXISTS (SELECT 1 FROM location_sources ls JOIN locations l ON l.id = ls.location_id
                             WHERE ls.source_code = s.code AND l.status = 'active')"""
        )
        attribs = await cur.fetchall()
    features = []
    for r in rows:
        props = {k: v for k, v in r.items() if k not in ("lon", "lat")}
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
            "properties": props,
        })
    payload = {
        "type": "FeatureCollection",
        "license": "ODbL-1.0 (OSM) + per-source attribution",
        "attribution": [a["attribution"] for a in attribs],
        "features": features,
    }
    headers = {"X-Data-Attribution": "; ".join(a["attribution"] for a in attribs)}
    return JSONResponse(content=jsonable_encoder(payload), headers=headers)
