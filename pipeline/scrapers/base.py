"""Scraper interface + shared loader. Every scraper yields NormalizedRecords; the loader
drives them all identically, honoring sources.storage_policy (ingest persists; enrich_only logs only)."""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from .. import dedup, store
from ..common import brand_key, normalize_house_number

log = logging.getLogger("opendrop.loader")


@dataclass
class NormalizedRecord:
    source_ref: str
    name: str
    org_type: str = "other"
    org_name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    address_line: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    hours: Optional[dict] = None
    hours_raw: Optional[str] = None
    accepted_items: Optional[list] = None
    phone: Optional[str] = None
    website: Optional[str] = None


class BaseScraper(ABC):
    code: str

    @abstractmethod
    def fetch(self, region) -> Iterable[NormalizedRecord]:
        ...


# Continental + AK/HI/PR-ish bounding box; rejects 0,0 and swapped lat/lon.
_US = (17.5, -180.0, 72.0, -64.0)  # (south, west, north, east)

# Closure-detection guardrails. A truncated/blocked upstream response (the API returned 3 of
# its usual 800 sites, got rate-limited, or changed schema) makes almost every existing link
# look "absent" — and the naive reconcile would then mass-retire a whole region. We refuse to
# reconcile when a run saw too few records, or when it would retire more than a fraction of a
# source's current in-region links. Both are env-overridable for a deliberate operator re-sync.
_RECONCILE_MAX_FRACTION = float(os.environ.get("RECONCILE_MAX_FRACTION", "0.40"))
_RECONCILE_MIN_SEEN = int(os.environ.get("RECONCILE_MIN_SEEN", "5"))


def _in_us(lat, lon) -> bool:
    return lat is not None and lon is not None and _US[0] <= lat <= _US[2] and _US[1] <= lon <= _US[3]


# The 50 states + DC — the same universe as data/us_zips.csv (our data-driven region source). The DB
# CHECK only enforces the two-letter *format*, so an upstream feed can hand us a well-formed but bogus
# code (satruck.org returned "PE" for Pennsylvania ARCs). Reject by membership, not just shape, so a
# bad code becomes NULL (and the coord-based backfill can recover it) instead of a phantom "state".
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
    "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)


def _enrich(rec: NormalizedRecord) -> dict:
    d = asdict(rec)
    d["brand_key"] = brand_key(rec.org_name, rec.name)
    d["house_number"] = normalize_house_number(rec.address_line)
    s = (d.get("state") or "").strip().upper()
    d["state"] = s if s in US_STATES else None  # else NULL (bogus/foreign code -> let backfill recover)
    return d


def _match_dict(d: dict) -> dict:
    return {k: d.get(k) for k in ("lat", "lon", "name", "brand_key", "org_type", "house_number")}


def _reconcile(conn, code, policy, region, seen) -> int:
    """Closure/deletion detection: drop this ingest source's links *within the region's bbox*
    that were NOT seen in this run (the source no longer lists them). Scoped to the region so a
    partial/regional sweep can't mass-delete other regions. The source-delete trigger then
    recomputes affected locations' confidence (a row with no ingest source falls below the floor).
    Only runs for ingest sources with a non-empty result set (a 0-result run never deletes).

    Circuit breaker: count the source's *current* in-region links and how many this run would
    retire BEFORE deleting anything. If the run saw fewer than `_RECONCILE_MIN_SEEN` records, or
    would retire more than `_RECONCILE_MAX_FRACTION` of those links, we treat it as a truncated/
    blocked upstream response (not a wave of real closures) and skip — keeping a stale link is far
    cheaper to recover from than silently deleting a region's worth of live ones."""
    if policy != "ingest" or not seen or not hasattr(region, "bbox"):
        return 0
    s, w, n, e = region.bbox
    env = (w, s, e, n)  # ST_MakeEnvelope(xmin=lon_w, ymin=lat_s, xmax=lon_e, ymax=lat_n)

    current = conn.execute(
        """SELECT count(*) AS n FROM location_sources
           WHERE source_code = %s AND source_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)""",
        (code, *env),
    ).fetchone()["n"]
    if current == 0:
        return 0

    would = conn.execute(
        """SELECT count(*) AS n FROM location_sources
           WHERE source_code = %s
             AND source_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
             AND NOT (source_ref::text = ANY(%s))""",
        (code, *env, list(seen)),
    ).fetchone()["n"]
    if would == 0:
        return 0

    if len(seen) < _RECONCILE_MIN_SEEN or would > current * _RECONCILE_MAX_FRACTION:
        log.warning(
            "%s: SKIPPING closure-detection — run would retire %d of %d in-region link(s) "
            "(seen=%d, max_fraction=%.0f%%, min_seen=%d). Looks like a truncated/blocked upstream "
            "response, not real closures. Set RECONCILE_MAX_FRACTION / RECONCILE_MIN_SEEN to force.",
            code, would, current, len(seen), _RECONCILE_MAX_FRACTION * 100, _RECONCILE_MIN_SEEN,
        )
        return 0

    rows = conn.execute(
        """DELETE FROM location_sources
           WHERE source_code = %s
             AND source_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
             AND NOT (source_ref::text = ANY(%s))
           RETURNING location_id""",
        (code, *env, list(seen)),
    ).fetchall()
    conn.commit()
    if rows:
        log.info("%s: reconciled %d of %d in-region location-link(s) as closed/absent", code, len(rows), current)
    return len(rows)


def load(scraper: BaseScraper, region, conn) -> dict:
    src = store.get_source(conn, scraper.code)
    if src is None:
        raise ValueError(f"unknown source code: {scraper.code}")
    policy = src["storage_policy"]
    log_id = store.start_scrape_log(conn, scraper.code)
    fetched = upserted = new = enrich = skipped = rejected = errors = removed = 0
    seen: set[str] = set()
    try:
        for rec in scraper.fetch(region):
            fetched += 1
            if rec.lat is None or rec.lon is None:
                skipped += 1
                continue
            if not _in_us(rec.lat, rec.lon):
                rejected += 1  # data-quality gate: 0,0 / swapped / out-of-country coords
                continue
            d = _enrich(rec)
            md = _match_dict(d)

            if policy == "enrich_only":
                if dedup.find_match(conn, md) is not None:
                    enrich += 1
                continue  # persist NOTHING (D1)

            # Per-record isolation: one bad record (e.g. a constraint violation from dirty
            # upstream data) is skipped, not fatal to the whole source.
            try:
                existing = conn.execute(
                    "SELECT location_id FROM location_sources WHERE source_code=%s AND source_ref=%s",
                    (scraper.code, rec.source_ref),
                ).fetchone()
                if existing:
                    loc_id = existing["location_id"]
                    store.upsert_source(conn, loc_id, scraper.code, rec.source_ref, rec.lon, rec.lat, rec.name, d)
                    store.refresh_location_fields(conn, loc_id)
                    upserted += 1
                else:
                    match = dedup.find_match(conn, md)
                    if match is not None:
                        store.upsert_source(conn, match, scraper.code, rec.source_ref, rec.lon, rec.lat, rec.name, d)
                        store.refresh_location_fields(conn, match)
                        upserted += 1
                    else:
                        loc_id = store.insert_location(conn, d)
                        store.upsert_source(conn, loc_id, scraper.code, rec.source_ref, rec.lon, rec.lat, rec.name, d)
                        new += 1
                seen.add(rec.source_ref)
                conn.commit()
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                errors += 1
                log.warning("%s: skipped bad record %s: %s", scraper.code, rec.source_ref, e)

        # Only reconcile (closure-detect) on a clean run — record errors mean `seen` is
        # incomplete, so deleting "absent" links could falsely retire live locations.
        if errors == 0:
            removed = _reconcile(conn, scraper.code, policy, region, seen)
        else:
            log.warning("%s: skipping closure-detection (%d record errors -> incomplete run)", scraper.code, errors)
        status = "partial" if (errors or rejected) else "success"
        detail = {"skipped_no_coords": skipped, "rejected": rejected, "errors": errors, "removed": removed}
        if policy == "enrich_only":
            detail["enrich_matches"] = enrich
        store.finish_scrape_log(conn, log_id, status, fetched, upserted, new, removed, detail)
        log.info("%s: fetched=%d new=%d upserted=%d enrich=%d skipped=%d rejected=%d errors=%d removed=%d",
                 scraper.code, fetched, new, upserted, enrich, skipped, rejected, errors, removed)
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        store.finish_scrape_log(conn, log_id, "failed", fetched, upserted, new, 0, error=str(e))
        raise
    return {"fetched": fetched, "new": new, "upserted": upserted, "enrich": enrich,
            "skipped": skipped, "rejected": rejected, "errors": errors, "removed": removed}
