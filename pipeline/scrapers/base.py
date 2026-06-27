"""Scraper interface + shared loader. Every scraper yields NormalizedRecords; the loader
drives them all identically, honoring sources.storage_policy (ingest persists; enrich_only logs only)."""
from __future__ import annotations

import logging
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


def _enrich(rec: NormalizedRecord) -> dict:
    d = asdict(rec)
    d["brand_key"] = brand_key(rec.org_name, rec.name)
    d["house_number"] = normalize_house_number(rec.address_line)
    if d.get("state"):
        d["state"] = d["state"].upper()[:2]
    return d


def _match_dict(d: dict) -> dict:
    return {k: d.get(k) for k in ("lat", "lon", "name", "brand_key", "org_type", "house_number")}


def _reconcile(conn, code, policy, region, seen) -> int:
    """Closure/deletion detection: drop this ingest source's links *within the region's bbox*
    that were NOT seen in this run (the source no longer lists them). Scoped to the region so a
    partial/regional sweep can't mass-delete other regions. The source-delete trigger then
    recomputes affected locations' confidence (a row with no ingest source falls below the floor).
    Only runs for ingest sources with a non-empty result set (a 0-result run never deletes)."""
    if policy != "ingest" or not seen or not hasattr(region, "bbox"):
        return 0
    s, w, n, e = region.bbox
    rows = conn.execute(
        """DELETE FROM location_sources
           WHERE source_code = %s
             AND source_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
             AND NOT (source_ref::text = ANY(%s))
           RETURNING location_id""",
        (code, w, s, e, n, list(seen)),
    ).fetchall()
    conn.commit()
    if rows:
        log.info("%s: reconciled %d closed/absent location-link(s) in region", code, len(rows))
    return len(rows)


def load(scraper: BaseScraper, region, conn) -> dict:
    src = store.get_source(conn, scraper.code)
    if src is None:
        raise ValueError(f"unknown source code: {scraper.code}")
    policy = src["storage_policy"]
    log_id = store.start_scrape_log(conn, scraper.code)
    fetched = upserted = new = enrich = skipped = 0
    seen: set[str] = set()
    try:
        for rec in scraper.fetch(region):
            fetched += 1
            if rec.lat is None or rec.lon is None:
                skipped += 1
                continue
            d = _enrich(rec)
            md = _match_dict(d)

            if policy == "enrich_only":
                if dedup.find_match(conn, md) is not None:
                    enrich += 1
                continue  # persist NOTHING (D1)

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

        removed = _reconcile(conn, scraper.code, policy, region, seen)
        detail = {"skipped_no_coords": skipped, "removed": removed}
        if policy == "enrich_only":
            detail["enrich_matches"] = enrich
        store.finish_scrape_log(conn, log_id, "success", fetched, upserted, new, removed, detail)
        log.info("%s: fetched=%d new=%d upserted=%d enrich=%d skipped=%d removed=%d",
                 scraper.code, fetched, new, upserted, enrich, skipped, removed)
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        store.finish_scrape_log(conn, log_id, "failed", fetched, upserted, new, 0, error=str(e))
        raise
    return {"fetched": fetched, "new": new, "upserted": upserted, "enrich": enrich, "skipped": skipped}
