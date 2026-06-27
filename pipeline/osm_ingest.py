"""OSM ingest via Overpass (batch only — never called from the API). Falls back to the
committed Phase-1 fixture so seeding is deterministic when Overpass is unreachable."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from .scrapers.base import BaseScraper, NormalizedRecord, load

log = logging.getLogger("opendrop.osm")

FIXTURE = Path(__file__).resolve().parents[1] / "research" / "data" / "osm_columbus.json"
DEFAULT_BBOX = os.environ.get("SEED_REGION_BBOX", "39.80,-83.25,40.18,-82.75")  # s,w,n,e


def _query(bbox: str) -> str:
    s, w, n, e = bbox.split(",")
    b = f"{s},{w},{n},{e}"
    return f"""[out:json][timeout:90];
(
  nwr["shop"="charity"]({b});
  nwr["shop"="second_hand"]({b});
  nwr["amenity"="recycling"]["recycling:clothes"]({b});
  nwr["amenity"="recycling"]["recycling:shoes"]({b});
);
out center tags;"""


def _org_type(tags: dict) -> str:
    if tags.get("shop") == "charity":
        return "charity_store"
    if tags.get("shop") == "second_hand":
        return "thrift_store"
    if tags.get("amenity") == "recycling" and ("recycling:clothes" in tags or "recycling:shoes" in tags):
        return "drop_bin"
    return "other"


def _to_record(el: dict) -> NormalizedRecord | None:
    tags = el.get("tags", {})
    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    if lat is None or lon is None:
        return None
    org_type = _org_type(tags)
    org_name = tags.get("brand") or tags.get("operator")
    name = tags.get("name") or org_name or ("Clothing donation bin" if org_type == "drop_bin" else "Donation location")
    hn = tags.get("addr:housenumber")
    st = tags.get("addr:street")
    address_line = " ".join(x for x in (hn, st) if x) or None
    return NormalizedRecord(
        source_ref=f"{el['type']}/{el['id']}",
        name=name,
        org_type=org_type,
        org_name=org_name,
        lat=float(lat),
        lon=float(lon),
        address_line=address_line,
        city=tags.get("addr:city"),
        state=(tags.get("addr:state") or None),
        postal_code=tags.get("addr:postcode"),
        hours_raw=tags.get("opening_hours") or tags.get("collection_times"),
        website=tags.get("website") or tags.get("contact:website"),
        phone=tags.get("phone") or tags.get("contact:phone"),
    )


class OsmScraper(BaseScraper):
    code = "osm"

    def fetch(self, region):
        bbox = region or DEFAULT_BBOX
        data = self._overpass(bbox)
        if data is None:
            log.warning("Overpass unavailable; using committed fixture %s", FIXTURE)
            data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for el in data.get("elements", []):
            rec = _to_record(el)
            if rec is not None:
                yield rec

    @staticmethod
    def _overpass(bbox: str):
        url = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
        try:
            r = httpx.post(url, content=_query(bbox).encode(),
                           headers={"User-Agent": "OpenDrop/0.1 (civic open-data)"}, timeout=120)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("Overpass fetch failed: %s", e)
            return None


def main():
    from . import db
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    try:
        load(OsmScraper(), DEFAULT_BBOX, conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
