"""OSM ingest via Overpass (batch only — never called from the API).

For national coverage a region bbox can be far too large for one Overpass query, so the bbox is
split into <= OSM_TILE_DEGREES tiles, each queried (politely, with backoff) and merged/deduped by
(type, id). Falls back to the committed Phase-1 fixture so seeding stays deterministic when
Overpass is unreachable — but only when the region actually covers the fixture's metro (Columbus),
so a national sweep never injects Columbus bins into, say, Montana."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .scrapers.base import BaseScraper, NormalizedRecord, load
from .scrapers.http import PoliteClient

log = logging.getLogger("opendrop.osm")

FIXTURE = Path(__file__).resolve().parents[1] / "research" / "data" / "osm_columbus.json"
DEFAULT_BBOX = os.environ.get("SEED_REGION_BBOX", "39.80,-83.25,40.18,-82.75")  # s,w,n,e
_FIXTURE_CENTER = (39.96, -82.99)  # Columbus — the fixture's metro


def _tile_degrees() -> float:
    try:
        d = float(os.environ.get("OSM_TILE_DEGREES", "3.0"))
    except (TypeError, ValueError):
        d = 3.0
    return d if d > 0 else 3.0


def _tiles(bbox, step: float):
    """Split (s, w, n, e) into <= step-degree tiles, walking south->north, west->east.
    A bbox smaller than one tile yields itself."""
    s, w, n, e = bbox
    lat = s
    while lat < n:
        lat2 = min(lat + step, n)
        lon = w
        while lon < e:
            lon2 = min(lon + step, e)
            yield (lat, lon, lat2, lon2)
            lon = lon2
        lat = lat2


def _region_bbox(region):
    if hasattr(region, "bbox"):
        return tuple(region.bbox)
    s, w, n, e = (float(x) for x in (region or DEFAULT_BBOX).split(","))
    return (s, w, n, e)


def _covers_fixture(bbox) -> bool:
    s, w, n, e = bbox
    la, lo = _FIXTURE_CENTER
    return s <= la <= n and w <= lo <= e


def _query(bbox) -> str:
    s, w, n, e = bbox
    b = f"{s},{w},{n},{e}"
    return f"""[out:json][timeout:90];
(
  nwr["shop"="charity"]({b});
  nwr["shop"="second_hand"]({b});
  nwr["amenity"="recycling"]["recycling:clothes"]({b});
  nwr["amenity"="recycling"]["recycling:shoes"]({b});
);
out center tags;"""


# Resale / buy-back chains and keywords — these BUY your clothes (vs donation-only thrift).
_RESALE_HINTS = (
    "consignment", "resale", "buy sell trade", "buy-sell-trade",
    "plato's closet", "platos closet", "buffalo exchange", "crossroads trading",
    "uptown cheapskate", "once upon a child", "clothes mentor", "style encore",
    "play it again", "kid to kid", "music go round",
)


def _org_type(tags: dict) -> str:
    shop = tags.get("shop")
    if shop == "charity":
        return "charity_store"
    if shop in ("second_hand", "consignment"):
        name = " ".join(filter(None, (tags.get("name"), tags.get("brand"), tags.get("operator")))).lower()
        if shop == "consignment" or tags.get("second_hand") == "no" or any(h in name for h in _RESALE_HINTS):
            return "consignment"
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
        bbox = _region_bbox(region)
        elements = self._collect(bbox)
        if elements is None:
            # Every tile failed (Overpass unreachable). Use the committed fixture ONLY for regions
            # that actually cover its metro, so a national run never seeds Columbus bins elsewhere.
            if _covers_fixture(bbox):
                log.warning("Overpass unavailable; using committed fixture %s", FIXTURE)
                elements = json.loads(FIXTURE.read_text(encoding="utf-8")).get("elements", [])
            else:
                log.warning("Overpass unavailable for bbox %s; no OSM records this run", bbox)
                elements = []
        seen: set[tuple] = set()
        for el in elements:
            key = (el.get("type"), el.get("id"))
            if key in seen:
                continue  # the same node can fall in two adjacent tiles
            seen.add(key)
            rec = _to_record(el)
            if rec is not None:
                yield rec

    def _collect(self, bbox):
        """Tile the bbox, query each cell politely, and merge elements. Returns the element list,
        or None if EVERY tile failed (so the caller can decide whether to fall back to the fixture).
        A run where at least one tile succeeded — even with zero elements — returns a list."""
        url = os.environ.get("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
        tiles = list(_tiles(bbox, _tile_degrees()))
        elements: list = []
        any_ok = False
        with PoliteClient(timeout=180, headers={"User-Agent": "OpenDrop/0.1 (civic open-data)"}) as client:
            for i, tb in enumerate(tiles, 1):
                data = self._overpass(client, url, tb)
                if data is None:
                    # A dead tile means `seen` is missing that tile's nodes; reconciling against an
                    # incomplete sweep would falsely retire live bins in the failed tile. Flag it.
                    self.fetch_failures += 1
                    continue
                any_ok = True
                got = data.get("elements", [])
                elements.extend(got)
                if len(tiles) > 1:
                    log.info("osm tile %d/%d %s -> %d elements", i, len(tiles), tb, len(got))
        return elements if any_ok else None

    @staticmethod
    def _overpass(client, url, bbox):
        try:
            r = client.post(url, content=_query(bbox).encode())
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            log.warning("Overpass tile %s failed: %s", bbox, e)
            return None


def main():
    from . import db
    from .regions import get_region
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    try:
        load(OsmScraper(), get_region(), conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
