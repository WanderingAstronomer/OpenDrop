from fastapi import APIRouter, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from .. import db
from ..config import BUCKETS, settings
from ..geocode import reverse as geo_reverse
from ..geocode import search as geo_search

router = APIRouter()


@router.get("/geosearch")
async def geosearch(q: str = Query(min_length=3, max_length=120)):
    """Free-text place/address search (Nominatim proxy + cache) powering the map search box."""
    return {"results": await geo_search(q.strip())}


@router.get("/reverse")
async def reverse_geocode(lat: float = Query(ge=-90, le=90), lon: float = Query(ge=-180, le=180)):
    """Reverse-geocode a dropped pin → structured address, for the Add-location form."""
    return {"address": await geo_reverse(lat, lon)}


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
        # Coverage envelope of the live data — drives the map's initial view so it always opens
        # framed on wherever the data actually is (one metro, a state, or the whole US), instead of
        # a hardcoded region. NULL extent (no active rows yet) => client uses its national fallback.
        cur = await conn.execute(
            """SELECT ST_YMin(ext) AS s, ST_XMin(ext) AS w, ST_YMax(ext) AS n, ST_XMax(ext) AS e
               FROM (SELECT ST_Extent(geom) AS ext FROM locations WHERE status='active') q"""
        )
        ext = await cur.fetchone()
    coverage = None
    if ext and ext["s"] is not None:
        s, w, n, e = ext["s"], ext["w"], ext["n"], ext["e"]
        coverage = {"bbox": [s, w, n, e], "center": [(s + n) / 2, (w + e) / 2]}
    return {
        "counts": {"active": counts.get("active", 0), "pending": counts.get("pending", 0), "by_type": by_type},
        "sources": sources,
        "turnstile_sitekey": settings.turnstile_sitekey,
        "confidence_buckets": BUCKETS,
        # Initial-view hint (data-driven bbox of active locations; null until seeded).
        "coverage": coverage,
        # Client constants for the correction flow (single source of truth = the API).
        "gps_radius_m": settings.gps_corroboration_radius_m,
        "correction_max_move_m": settings.correction_max_move_m,
    }


@router.get("/orgs")
async def orgs():
    """Known organization/brand names, for the 'whose donation bin is this?' dropdown in the
    field-correction UI. Distinct org_names already on active locations, most-common first; the
    UI also offers a free-text 'add a new org' for drives that aren't represented yet."""
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT org_name, count(*) AS n FROM locations
               WHERE status = 'active' AND org_name IS NOT NULL AND btrim(org_name) <> ''
               GROUP BY org_name ORDER BY n DESC, org_name ASC LIMIT 500"""
        )
        names = [r["org_name"] for r in await cur.fetchall()]
    return {"orgs": names}


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
        # Hard row cap so the public dump can't be turned into an unbounded full-table scan. The
        # national active set is ~15k today; the default cap leaves generous headroom. A state
        # filter keeps responses small; an unfiltered dump is truncated at the cap (documented).
        cur = await conn.execute(
            f"""SELECT id, ST_X(geom) AS lon, ST_Y(geom) AS lat, name, org_type, org_name,
                       address_line, city, state, postal_code, hours, accepted_items, phone, website,
                       confidence, last_verified_at
                FROM v_public_locations {where}
                ORDER BY id
                LIMIT %s""",
            params + [settings.export_max_rows],
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
    # HTTP headers must be ASCII; the in-payload `attribution` keeps the proper ©.
    header_val = "; ".join(a["attribution"] for a in attribs).replace("©", "(c)")
    headers = {"X-Data-Attribution": header_val.encode("ascii", "ignore").decode("ascii")}
    return JSONResponse(content=jsonable_encoder(payload), headers=headers)
