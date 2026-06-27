"""Deduplication — the Phase-1-validated predicate + idempotent merge.

Predicate (FINDINGS Finding 4 / ARCHITECTURE §7.4):
  match := brand_equal AND ( (<=300m AND name_sim>=0.4)
                             OR (<=600m AND name_sim>=0.4 AND house_number_equal) )
           OR (both unbranded AND same org_type in {drop_bin,donation_center} AND <=25m)
"""
from __future__ import annotations

import logging

from .common import brand_equal, haversine_m, name_sim

log = logging.getLogger("opendrop.dedup")
_BIN_TYPES = {"drop_bin", "donation_center"}


def is_match(a: dict, b: dict) -> bool:
    if a.get("lat") is None or b.get("lat") is None:
        return False
    dist = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    if dist > 600:
        return False
    ns = name_sim(a.get("name"), b.get("name"))
    if brand_equal(a.get("brand_key"), b.get("brand_key")):
        if dist <= 300 and ns >= 0.4:
            return True
        hn = a.get("house_number")
        if dist <= 600 and ns >= 0.4 and hn is not None and hn == b.get("house_number"):
            return True
        return False
    # unbranded co-located bins
    if a.get("brand_key") is None and b.get("brand_key") is None:
        if (a.get("org_type") == b.get("org_type")
                and a.get("org_type") in _BIN_TYPES and dist <= 25):
            return True
    return False


def _candidates(conn, lon, lat):
    return conn.execute(
        """SELECT id, name, brand_key, org_type, house_number, ST_X(geom) AS lon, ST_Y(geom) AS lat
           FROM locations
           WHERE status NOT IN ('merged','hidden')
             AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography, 600)""",
        (lon, lat),
    ).fetchall()


def find_match(conn, rec: dict, exclude_id: int | None = None) -> int | None:
    """Return the id of an existing canonical location matching rec, or None."""
    for c in _candidates(conn, rec["lon"], rec["lat"]):
        if exclude_id is not None and c["id"] == exclude_id:
            continue
        if is_match(rec, dict(c)):
            return c["id"]
    return None


def _authority_sum(conn, loc_id) -> int:
    row = conn.execute(
        """SELECT COALESCE(SUM(s.authority_weight),0) AS a
           FROM location_sources ls JOIN sources s ON s.code = ls.source_code AND s.storage_policy='ingest'
           WHERE ls.location_id = %s""",
        (loc_id,),
    ).fetchone()
    return row["a"]


def choose_canonical(conn, a: int, b: int) -> tuple[int, int]:
    """Keep = higher Σ authority, tie-break older (smaller id)."""
    aa, ab = _authority_sum(conn, a), _authority_sum(conn, b)
    if (aa, -a) >= (ab, -b):
        return a, b
    return b, a


def merge(conn, keep: int, loser: int):
    from .store import refresh_location_fields  # lazy: keeps the pure predicate importable w/o a DB driver

    # repoint loser's sources (trg_after_source recomputes keep)
    conn.execute("UPDATE location_sources SET location_id = %s WHERE location_id = %s", (keep, loser))
    # chain-compact any pointers aimed at the loser
    conn.execute("UPDATE locations SET merged_into_id = %s WHERE merged_into_id = %s", (keep, loser))
    # tombstone the loser
    conn.execute(
        "UPDATE locations SET status='merged', merged_into_id=%s, source_count=0, updated_at=now() WHERE id=%s",
        (keep, loser),
    )
    refresh_location_fields(conn, keep)


def run(conn) -> int:
    """Global pairwise dedup pass over active/pending locations. Returns merges performed."""
    pairs = conn.execute(
        """SELECT a.id AS a, b.id AS b
           FROM locations a JOIN locations b
             ON a.id < b.id
            AND a.status IN ('active','pending') AND b.status IN ('active','pending')
            AND ST_DWithin(a.geom::geography, b.geom::geography, 600)"""
    ).fetchall()
    rows = conn.execute(
        """SELECT id, name, brand_key, org_type, house_number, ST_X(geom) AS lon, ST_Y(geom) AS lat
           FROM locations WHERE status IN ('active','pending')"""
    ).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}

    merged: set[int] = set()
    count = 0
    for p in pairs:
        a, b = p["a"], p["b"]
        if a in merged or b in merged:
            continue
        ra, rb = by_id.get(a), by_id.get(b)
        if not ra or not rb:
            continue
        if is_match(ra, rb):
            keep, loser = choose_canonical(conn, a, b)
            merge(conn, keep, loser)
            merged.add(loser)
            count += 1
    conn.commit()
    log.info("dedup: %d merge(s)", count)
    return count
