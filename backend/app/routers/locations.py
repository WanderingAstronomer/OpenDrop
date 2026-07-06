from fastapi import APIRouter, HTTPException, Query, Request

from pipeline.common import brand_key, name_sim, normalize_house_number

from .. import db
from ..community import (
    attribute_aggregates,
    engagement_tier,
    required_support,
    retire_deny_floor,
)
from ..config import bucket, settings
from ..deps import client_ip
from ..geocode import geocode, reverse
from ..models import AddressIn, SubmitIn
from ..moderation import screen_submission
from ..security import ip_hash, token_hash, verify_turnstile

router = APIRouter()

_POINT = "ST_SetSRID(ST_MakePoint(%s, %s), 4326)"  # args: (lon, lat)

# Density engine (B8). At cluster zooms we return one bubble per "cell"; the cell size is derived
# from the map ZOOM (slippy-tile math) so on-screen bubble density is CONSTANT at every zoom and on
# every device — instead of the old bbox_span/32 that always cut ~32 cells across the viewport
# (confetti at every zoom). TARGET_PX is the intended on-screen cell edge in CSS px; the frontend
# caps bubble diameter below it so grid bubbles never touch.
CLUSTER_TARGET_PX = 82.0
# At or below this zoom the view spans several states — aggregate GROUP BY state ("one bubble per
# state", the owner's ask for the first zoom-ins) instead of a grid. Above it, the zoom-aware grid.
STATE_BAND_MAX_Z = 6.0
# Web-Mercator world width in CSS px at zoom z is 256 * 2**z, covering 360°. One TARGET_PX-wide cell
# is therefore this many degrees. Floored so a degenerate tiny cell can't be requested.
def cluster_cell_deg(z: float) -> float:
    return max(360.0 / (2.0 ** z) * (CLUSTER_TARGET_PX / 256.0), 0.005)


@router.get("/locations")
async def list_locations(bbox: str, types: str | None = None,
                         min_confidence: float = Query(0.0, ge=0, le=100), cluster: str = "auto",
                         z: float | None = Query(None, ge=0, le=24)):
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
        if cluster == "off":
            use_points = True
        elif cluster == "on":
            use_points = False
        else:
            # We only need to know whether the in-bbox active set EXCEEDS point_cap, not the exact
            # total, so cap the scan at point_cap+1 rows. The map polls this on every pan/zoom; an
            # unbounded count(*) over the (national) active set on each call is needless DB load.
            cur = await conn.execute(
                f"SELECT count(*) AS n FROM (SELECT 1 FROM locations WHERE {where} LIMIT %s) t",
                params + [settings.point_cap + 1])
            use_points = (await cur.fetchone())["n"] <= settings.point_cap

        if use_points:
            # has_pending: any OPEN community proposal (pin move or detail change) awaiting
            # confirmations — the map pulses these so contributors can find spots needing a vote.
            # Both EXISTS probes ride the partial *_open_ix indexes.
            cur = await conn.execute(
                f"""SELECT id, name, org_type, confidence, ST_X(geom) AS lon, ST_Y(geom) AS lat,
                           (EXISTS (SELECT 1 FROM location_corrections c
                                     WHERE c.location_id = locations.id AND c.status = 'open')
                            OR EXISTS (SELECT 1 FROM field_corrections f
                                        WHERE f.location_id = locations.id AND f.status = 'open')
                           ) AS has_pending
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
                                   "confidence": conf, "bucket": bucket(conf),
                                   "has_pending": bool(r["has_pending"])},
                })
            return {"mode": "points", "type": "FeatureCollection", "features": features}

        # Cluster mode, two tiers (B8). The map passes its zoom `z`; from it we pick the aggregation.
        #  - z is None (a client that predates the density engine): keep the legacy bbox_span/32 grid
        #    so nothing regresses during the deploy window (state band + zoom cell need z).
        #  - z <= STATE_BAND_MAX_Z (wide, several states): ONE BUBBLE PER STATE at the state's data
        #    centroid; the ~0.4% of rows with no state fall back to a coarse grid cell so they stay
        #    visible and don't pile onto one phantom centroid.
        #  - otherwise: a ZOOM-AWARE grid — cell = f(z), snapped to grid VERTICES (evenly spaced by
        #    the cell, so bubbles are de-overlapped when the frontend caps their diameter < cell).
        if z is None:
            tier = "grid"
            cell = max(max(east - west, north - south) / 32.0, 0.005)
            cur = await conn.execute(
                f"""SELECT avg(ST_X(geom)) AS lon, avg(ST_Y(geom)) AS lat,
                           count(*) AS cnt, avg(confidence) AS ac
                    FROM locations WHERE {where}
                    GROUP BY ST_SnapToGrid(geom, %s, %s) LIMIT %s""",
                params + [cell, cell, settings.cluster_cap],
            )
        elif z <= STATE_BAND_MAX_Z:
            tier = "state"
            cell = cluster_cell_deg(z)  # only the null-state fallback grid uses it
            # ORDER BY count DESC before the cap: if a view ever exceeds cluster_cap groups, the
            # DENSEST survive and only the sparsest are dropped (a graceful truncation, not arbitrary).
            cur = await conn.execute(
                f"""SELECT avg(ST_X(geom)) AS lon, avg(ST_Y(geom)) AS lat,
                           count(*) AS cnt, avg(confidence) AS ac
                    FROM locations WHERE {where}
                    GROUP BY COALESCE(NULLIF(state, ''), ST_AsText(ST_SnapToGrid(geom, %s, %s)))
                    ORDER BY count(*) DESC LIMIT %s""",
                params + [cell, cell, settings.cluster_cap],
            )
        else:
            tier = "grid"
            cell = cluster_cell_deg(z)
            cur = await conn.execute(
                f"""SELECT ST_X(g) AS lon, ST_Y(g) AS lat, count(*) AS cnt, avg(confidence) AS ac
                    FROM (SELECT ST_SnapToGrid(geom, %s, %s) AS g, confidence
                          FROM locations WHERE {where}) t
                    GROUP BY g ORDER BY count(*) DESC LIMIT %s""",
                [cell, cell] + params + [settings.cluster_cap],
            )
        clusters = [{"lon": float(r["lon"]), "lat": float(r["lat"]),
                     "count": r["cnt"], "avg_confidence": round(float(r["ac"]), 1)}
                    for r in await cur.fetchall()]
        return {"mode": "clusters", "tier": tier, "clusters": clusters}


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
        if r["status"] == "hidden":
            # Operator takedown (status='hidden' is a sticky manual override that survives
            # recompute_confidence). Public detail must 404 — the map already filters it out by
            # status='active', this closes the direct-by-id read path too.
            raise HTTPException(404, {"code": "not_found", "message": "location not found"})
        cur = await conn.execute(
            """SELECT s.code, s.display_name, s.attribution
               FROM location_sources ls JOIN sources s ON s.code = ls.source_code
               WHERE ls.location_id = %s ORDER BY s.authority_weight DESC""",
            (loc_id,),
        )
        sources = [dict(s) for s in await cur.fetchall()]

        # Community signals: engagement tier, attribute aggregates, and any open pin corrections.
        cur = await conn.execute("SELECT location_engagement(%s) AS e", (loc_id,))
        engagement = (await cur.fetchone())["e"]
        attributes = await attribute_aggregates(conn, loc_id)
        # NOTE: gps_corroborated is deliberately NOT exposed per-correction. It still weights
        # consensus server-side, but publishing "this proposer was physically on site" per row
        # would correlate presence — the GPS privacy contract says only a boolean is ever sent to
        # us, and we don't re-publish even that. The support meter conveys all the UI needs.
        cur = await conn.execute(
            """SELECT id, suggested_lat, suggested_lon, note, image_id,
                      support, required_support, confirmations, rejections, created_at
               FROM location_corrections
               WHERE location_id = %s AND status = 'open'
               ORDER BY created_at DESC""",
            (loc_id,),
        )
        open_corrections = [
            {"id": c["id"], "suggested_lat": c["suggested_lat"], "suggested_lon": c["suggested_lon"],
             "note": c["note"], "image_id": c["image_id"],
             "support": c["support"], "required_support": c["required_support"],
             "confirmations": c["confirmations"], "rejections": c["rejections"],
             "created_at": c["created_at"]}
            for c in await cur.fetchall()
        ]
        # Open crowd field corrections (name / type / org / address) — migration 0009. The proposed
        # value is exposed so the popover can show "rename to X" and let others vote it through.
        cur = await conn.execute(
            """SELECT id, field, proposed_value, proposed_line, proposed_city, proposed_state,
                      proposed_postal, note, support, required_support, confirmations, rejections,
                      created_at
               FROM field_corrections
               WHERE location_id = %s AND status = 'open'
               ORDER BY created_at DESC""",
            (loc_id,),
        )
        open_field_corrections = [
            {"id": c["id"], "field": c["field"], "proposed_value": c["proposed_value"],
             "proposed_address": ({"line": c["proposed_line"], "city": c["proposed_city"],
                                   "state": c["proposed_state"], "postal_code": c["proposed_postal"]}
                                  if c["field"] == "address" else None),
             "note": c["note"], "support": c["support"], "required_support": c["required_support"],
             "confirmations": c["confirmations"], "rejections": c["rejections"],
             "created_at": c["created_at"]}
            for c in await cur.fetchall()
        ]

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
        # Engagement-tiered trust model (see migration 0006 / community.py).
        "engagement": engagement,
        "tier": engagement_tier(engagement),
        "required_support": required_support(engagement),
        "retire_deny_floor": retire_deny_floor(engagement),
        "attributes": attributes,
        "open_corrections": open_corrections,
        "open_field_corrections": open_field_corrections,
    }


@router.post("/locations")
async def submit_location(body: SubmitIn, request: Request):
    ip = client_ip(request)
    if not await verify_turnstile(body.turnstile_token, ip):
        raise HTTPException(403, {"code": "turnstile_failed", "message": "Turnstile verification failed"})

    a = body.address
    reason = screen_submission(body.name, a.line, a.city)
    if reason:
        raise HTTPException(422, {"code": "rejected_content", "message": reason})

    iph = ip_hash(ip)
    thash = token_hash(body.turnstile_token)
    bkey = brand_key(body.name)
    dropped_pin = body.lat is not None and body.lon is not None

    # Step 1 — per-IP rate limit. The connection is held only for this fast count and then released
    # BEFORE the external geocode below: an httpx round-trip must never occupy a pool connection, or
    # a burst of submits can exhaust the pool and stall every other request (the held-connection
    # blocker). Checking the limit first also means a throttled IP never triggers a Nominatim call.
    async with db.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM pending_locations "
            "WHERE submitter_ip_hash = %s AND created_at > now() - interval '1 day'",
            (iph,),
        )
        if (await cur.fetchone())["n"] >= settings.submit_per_ip_per_day:
            raise HTTPException(429, {"code": "submit_cooldown", "message": "daily submission limit reached"})

    # Step 2 — geocode / reverse-geocode OUTSIDE any held DB connection (external HTTP, no pool slot).
    if dropped_pin:
        # Drop-a-pin: the pin is authoritative; back-fill a missing address via reverse geocode.
        coords = (body.lat, body.lon)
        if not (a.line or a.city or a.postal_code):
            rev = await reverse(body.lat, body.lon)
            if rev:
                a = AddressIn(line=a.line or rev["line"], city=a.city or rev["city"],
                              state=a.state or rev["state"], postal_code=a.postal_code or rev["postal_code"])
    else:
        coords = await geocode(a.line, a.city, a.state, a.postal_code)
    state = (a.state or "").upper() or None

    # Step 3 — reacquire a connection for the dupe scan + write transaction (fast, DB-only work).
    async with db.pool.connection() as conn:
        # Classify a nearby match by visibility. An ACTIVE duplicate is a real, on-map collision
        # and we reject the re-add. A PENDING duplicate is a location that exists but is gated off
        # the map by low confidence — the user genuinely cannot see it, so re-adding it should
        # RESURFACE it (an implicit confirm vote), not dead-end as a duplicate of an invisible pin.
        active_dupe_id = None
        pending_dupe_id = None
        if coords:
            lat, lon = coords
            cur = await conn.execute(
                f"""SELECT id, name, brand_key, status FROM locations
                    WHERE status IN ('active', 'pending')
                      AND ST_DWithin(geom::geography, {_POINT}::geography, 300)""",
                (lon, lat),
            )
            for cand in await cur.fetchall():
                same_brand = bkey is not None and cand["brand_key"] == bkey
                if same_brand or name_sim(body.name, cand["name"]) >= 0.4:
                    if cand["status"] == "active":
                        active_dupe_id = cand["id"]
                        break  # a visible duplicate is the strongest signal — stop and reject
                    if pending_dupe_id is None:
                        pending_dupe_id = cand["id"]  # remember, but keep scanning for an active one
        dupe_id = active_dupe_id or pending_dupe_id  # recorded on the pending_locations audit row

        location_id = None
        now_active = False
        async with conn.transaction():
            # Serialize the per-IP daily cap with the write: the earlier count (Step 1) is a cheap
            # pre-geocode reject, but it releases its connection and an external geocode happens
            # before we get here, so a concurrent burst from one IP could otherwise all pass it. Take
            # a per-IP advisory lock and re-check the cap authoritatively inside the write txn.
            await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (iph,))
            cur = await conn.execute(
                "SELECT count(*) AS n FROM pending_locations "
                "WHERE submitter_ip_hash = %s AND created_at > now() - interval '1 day'",
                (iph,),
            )
            if (await cur.fetchone())["n"] >= settings.submit_per_ip_per_day:
                raise HTTPException(429, {"code": "submit_cooldown", "message": "daily submission limit reached"})
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

            if active_dupe_id:
                status = "duplicate"
            elif pending_dupe_id:
                # Re-adding a gated (invisible) location: treat it as a confirm vote that resurfaces
                # the existing pin instead of rejecting it. One confirm takes a fresh crowd pin from
                # confidence 20 → 25 → 'active' (trg_after_vote → recompute_confidence). Respect the
                # 24h per-IP vote cooldown so a repeat re-add can't stack boosts; serialize with the
                # same advisory lock the vote endpoint uses.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))", (f"{pending_dupe_id}:{iph}",))
                cur = await conn.execute(
                    "SELECT 1 FROM votes WHERE location_id = %s AND ip_hash = %s "
                    "AND created_at > now() - interval '24 hours' LIMIT 1",
                    (pending_dupe_id, iph),
                )
                if await cur.fetchone() is None:
                    await conn.execute(
                        "INSERT INTO votes (location_id, vote, ip_hash, turnstile_hash) "
                        "VALUES (%s, 'confirm', %s, %s)",
                        (pending_dupe_id, iph, thash),
                    )
                location_id = pending_dupe_id
                status = "resurfaced"
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

        # Did the resurface boost clear the confidence gate? (Read after commit, so the vote
        # trigger has recomputed.) A disputed pin with denies may stay gated — that's intended.
        if status == "resurfaced" and location_id is not None:
            cur = await conn.execute("SELECT status FROM locations WHERE id = %s", (location_id,))
            row = await cur.fetchone()
            now_active = bool(row and row["status"] == "active")

    return {"pending_id": pending_id, "status": status, "geocoded": bool(coords),
            "location_id": location_id, "duplicate_of": dupe_id, "now_active": now_active}
