"""Promote crowd submissions (pending_locations 'awaiting' + geocoded) into canonical
locations as a 'crowd' source. Runnable standalone or called inline by POST /api/locations."""
from __future__ import annotations

import logging

from . import db, dedup, store
from .common import brand_key, normalize_house_number

log = logging.getLogger("opendrop.promote")


def promote_pending(conn, p: dict) -> int | None:
    """Promote one awaiting pending row that has coords. Returns new location_id, or None
    (if it was a duplicate of an existing location)."""
    lon, lat = p["lon"], p["lat"]
    bkey = brand_key(p["name"])
    md = {"lat": lat, "lon": lon, "name": p["name"], "brand_key": bkey,
          "org_type": p["org_type"], "house_number": normalize_house_number(p["address_line"])}
    dupe = dedup.find_match(conn, md)
    if dupe is not None:
        conn.execute(
            "UPDATE pending_locations SET status='duplicate', dupe_candidate_id=%s, updated_at=now() WHERE id=%s",
            (dupe, p["id"]),
        )
        return None
    rec = {"lon": lon, "lat": lat, "name": p["name"], "org_type": p["org_type"], "brand_key": bkey,
           "address_line": p["address_line"], "house_number": md["house_number"],
           "city": p["city"], "state": p["state"], "postal_code": p["postal_code"]}
    loc_id = store.insert_location(conn, rec)
    store.upsert_source(conn, loc_id, "crowd", f"pending/{p['id']}", lon, lat, p["name"], rec)
    conn.execute(
        "UPDATE pending_locations SET status='promoted', promoted_location_id=%s, updated_at=now() WHERE id=%s",
        (loc_id, p["id"]),
    )
    return loc_id


def run(conn) -> int:
    rows = conn.execute(
        """SELECT id, name, org_type, address_line, city, state, postal_code,
                  ST_X(geom) AS lon, ST_Y(geom) AS lat
           FROM pending_locations WHERE status='awaiting' AND geom IS NOT NULL"""
    ).fetchall()
    n = 0
    for p in rows:
        try:
            if promote_pending(conn, dict(p)) is not None:
                n += 1
            conn.commit()
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            log.warning("promote %s failed: %s", p["id"], e)
    log.info("promoted %d submission(s)", n)
    return n


def main():
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    try:
        print(f"promoted {run(conn)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
