from fastapi import APIRouter, HTTPException, Request

from pipeline.common import brand_key, name_sim, normalize_house_number

from .. import db
from ..config import bucket, settings
from ..deps import client_ip
from ..geocode import geocode
from ..models import SubmitIn
from ..security import ip_hash, token_hash, verify_turnstile

router = APIRouter()

_POINT = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"  # args: (lon, lat)


@router.get("/locations")
async def list_locations(bbox: str, types: str | None = None,
                         min_confidence: float = 0.0, cluster: str = "auto"):
    try:
        west, south, east, north = (float(x) for x in bbox.split(","))
    except Exception:  # noqa: BLE001
        raise HTTPException(400, {"code": "bad_bbox", "message": "bbox must be 'west,south,east,north'"})
    if not (west < east and south < north
            and -180 <= west <= 180 and -180 <= east <= 180
            and -90 <= south <= 90 and -90 <= north <= 90):
        raise HTTPException(400, {"code": "bad_bbox", "message": "invalid bbox bounds"})

    type_list = [t for t in (types.split(",") if types else []) if t]
    where = ("status = 'active' AND geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326) "
             "AND confidence >= %s")
    params: list = [west, south, east, north, min_confidence]
    if type_list:
        where += " AND org_type = ANY(%s)"
        params.append(type_list)

    async with db.pool.connection() as conn:
        cur = await conn.execute(f"SELECT count(*) AS n FROM locations WHERE {where}", params)
        total = (await cur.fetchone())["n"]

        if cluster == "off":
            use_points = True
        elif cluster == "on":
            use_points = False
        else:
            use_points = total <= settings.point_cap

        if use_points:
            cur = await conn.execute(
                f"""SELECT id, name, org_type, confidence, ST_X(geom) AS lon, ST_Y(geom) AS lat
                    FROM locations WHERE {where} LIMIT %s""",
                params + [settings.point_cap],
            )
            features = []
            for r in await cur.fetchall():
                conf = float(r["confidence"])
                features.append({
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                    "properties": {"id": r["id"], "name": r["name"], "org_type": r["org_type"],
                                   "confidence": conf, "bucket": bucket(conf)},
                })
            return {"mode": "points", "type": "FeatureCollection", "features": features}

        # cluster mode: grid-aggregate via ST_SnapToGrid, return cell centroids
        cell = max(max(east - west, north - south) / 32.0, 0.005)
        cur = await conn.execute(
            f"""SELECT avg(ST_X(geom)) AS lon, avg(ST_Y(geom)) AS lat,
                       count(*) AS cnt, avg(confidence) AS ac
                FROM locations WHERE {where}
                GROUP BY ST_SnapToGrid(geom, %s, %s)
                LIMIT %s""",
            params + [cell, cell, settings.cluster_cap],
        )
        clusters = [{"lon": float(r["lon"]), "lat": float(r["lat"]),
                     "count": r["cnt"], "avg_confidence": round(float(r["ac"]), 1)}
                    for r in await cur.fetchall()]
        return {"mode": "clusters", "clusters": clusters}


@router.get("/locations/{loc_id}")
async def get_location(loc_id: int):
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            """SELECT id, name, org_type, org_name, address_line, city, state, postal_code,
                      hours, hours_raw, accepted_items, phone, website, confidence, status,
                      upvotes, denies, last_verified_at, merged_into_id,
                      ST_X(geom) AS lon, ST_Y(geom) AS lat
               FROM locations WHERE id = %s""",
            (loc_id,),
        )
        r = await cur.fetchone()
        if r is None:
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})
        if r["status"] == "merged":
            raise HTTPException(404, {"code": "merged", "message": "location merged into another",
                                      "details": {"canonical_id": r["merged_into_id"]}})
        cur = await conn.execute(
            """SELECT s.code, s.display_name, s.attribution
               FROM location_sources ls JOIN sources s ON s.code = ls.source_code
               WHERE ls.location_id = %s ORDER BY s.authority_weight DESC""",
            (loc_id,),
        )
        sources = [dict(s) for s in await cur.fetchall()]

    conf = float(r["confidence"])
    return {
        "id": r["id"], "name": r["name"], "org_type": r["org_type"], "org_name": r["org_name"],
        "lat": r["lat"], "lon": r["lon"],
        "address": {"line": r["address_line"], "city": r["city"], "state": r["state"],
                    "postal_code": r["postal_code"]},
        "hours": r["hours"], "hours_raw": r["hours_raw"], "accepted_items": r["accepted_items"],
        "phone": r["phone"], "website": r["website"],
        "confidence": conf, "bucket": bucket(conf), "status": r["status"],
        "upvotes": r["upvotes"], "denies": r["denies"], "last_verified_at": r["last_verified_at"],
        "sources": sources,
    }


@router.post("/locations")
async def submit_location(body: SubmitIn, request: Request):
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)
    a = body.address
    state = (a.state or "").upper() or None
    bkey = brand_key(body.name)

    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM pending_locations "
            "WHERE submitter_ip_hash = %s AND created_at > now() - interval '1 day'",
            (iph,),
        )
        if (await cur.fetchone())["n"] >= settings.submit_per_ip_per_day:
            raise HTTPException(429, {"code": "submit_cooldown", "message": "daily submission limit reached"})

        coords = await geocode(a.line, a.city, a.state, a.postal_code)
        dupe_id = None
        if coords:
            lat, lon = coords
            cur = await conn.execute(
                f"""SELECT id, name, brand_key FROM locations
                    WHERE status IN ('active', 'pending')
                      AND ST_DWithin(geom::geography, {_POINT}::geography, 300)""",
                (lon, lat),
            )
            for cand in await cur.fetchall():
                same_brand = bkey is not None and cand["brand_key"] == bkey
                if same_brand or name_sim(body.name, cand["name"]) >= 0.4:
                    dupe_id = cand["id"]
                    break

        location_id = None
        async with conn.transaction():
            if coords:
                lat, lon = coords
                cur = await conn.execute(
                    f"""INSERT INTO pending_locations
                        (name, org_type, address_line, city, state, postal_code, geom,
                         submitter_ip_hash, turnstile_hash, status, dupe_candidate_id)
                        VALUES (%s,%s,%s,%s,%s,%s,{_POINT},%s,%s,%s,%s) RETURNING id""",
                    (body.name, body.org_type, a.line, a.city, state, a.postal_code, lon, lat,
                     iph, thash, "duplicate" if dupe_id else "awaiting", dupe_id),
                )
            else:
                cur = await conn.execute(
                    """INSERT INTO pending_locations
                       (name, org_type, address_line, city, state, postal_code,
                        submitter_ip_hash, turnstile_hash, status)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'awaiting') RETURNING id""",
                    (body.name, body.org_type, a.line, a.city, state, a.postal_code, iph, thash),
                )
            pending_id = (await cur.fetchone())["id"]

            if dupe_id:
                status = "duplicate"
            elif coords:
                lat, lon = coords
                cur = await conn.execute(
                    f"""INSERT INTO locations
                        (geom, name, org_type, brand_key, address_line, house_number, city, state, postal_code)
                        VALUES ({_POINT},%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                    (lon, lat, body.name, body.org_type, bkey, a.line,
                     normalize_house_number(a.line), a.city, state, a.postal_code),
                )
                location_id = (await cur.fetchone())["id"]
                await conn.execute(
                    f"""INSERT INTO location_sources
                        (location_id, source_code, source_ref, source_geom, source_name)
                        VALUES (%s, 'crowd', %s, {_POINT}, %s)""",
                    (location_id, f"pending/{pending_id}", lon, lat, body.name),
                )
                await conn.execute(
                    "UPDATE pending_locations SET status='promoted', promoted_location_id=%s, updated_at=now() "
                    "WHERE id = %s",
                    (location_id, pending_id),
                )
                status = "promoted"
            else:
                status = "awaiting"

    return {"pending_id": pending_id, "status": status, "geocoded": bool(coords),
            "location_id": location_id, "duplicate_of": dupe_id}
