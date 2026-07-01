"""Wearable Collections — NYC GrowNYC greenmarket clothing drop-off (INGEST). FINDINGS Finding 2.
The /greenmarket page exposes site names but NO coordinates (Google cid links only), so we
geocode the known greenmarket sites independently via Nominatim (never Google geometry).
NYC-only: a coverage guard skips this scraper for regions that don't overlap NYC."""
from __future__ import annotations

import logging

from .base import BaseScraper, NormalizedRecord, load
from .http import NOMINATIM_MIN_DELAY_S, PoliteClient

log = logging.getLogger("opendrop.wearable_collections")

NYC_BBOX = (40.40, -74.30, 41.00, -73.65)  # (south, west, north, east)
NOMINATIM = "https://nominatim.openstreetmap.org/search"

# Known public GrowNYC greenmarket drop-off sites (FINDINGS) -> geocode query.
SITES = [
    ("Tompkins Square Park Greenmarket", "Tompkins Square Park, New York, NY"),
    ("79th Street Greenmarket", "West 79th Street & Columbus Avenue, New York, NY"),
    ("Tribeca Greenmarket", "Greenwich Street & Chambers Street, New York, NY"),
    ("Carroll Gardens Greenmarket", "Carroll Park, Brooklyn, NY"),
    ("Fort Greene Park Greenmarket", "Fort Greene Park, Brooklyn, NY"),
    ("Grand Army Plaza Greenmarket", "Grand Army Plaza, Brooklyn, NY"),
    ("McCarren Park Greenmarket", "McCarren Park, Brooklyn, NY"),
    ("Sunnyside Greenmarket", "Skillman Avenue & 42nd Street, Sunnyside, Queens, NY"),
]


def _overlaps(a, b) -> bool:
    return a[0] <= b[2] and a[2] >= b[0] and a[1] <= b[3] and a[3] >= b[1]


def _geocode(client, query):
    try:
        r = client.get(NOMINATIM, params={"format": "jsonv2", "limit": "1", "countrycodes": "us", "q": query})
        r.raise_for_status()
        d = r.json()
        return (float(d[0]["lat"]), float(d[0]["lon"])) if d else None
    except Exception:  # noqa: BLE001
        return None


class WearableCollectionsScraper(BaseScraper):
    code = "wearable_collections"

    def fetch(self, region):
        if not _overlaps(region.bbox, NYC_BBOX):
            log.info("wearable_collections: region does not overlap NYC; skipping")
            return
        # Nominatim's usage policy caps geocoding at 1 req/s — pin the client there regardless of
        # the global scraper delay so a national run can never violate it.
        with PoliteClient(timeout=20, delay_s=NOMINATIM_MIN_DELAY_S,
                          headers={"User-Agent": "OpenDrop/0.1 (civic open-data)"}) as client:
            for name, query in SITES:
                coords = _geocode(client, query)
                if not coords:
                    self.fetch_failures += 1  # a site we couldn't place -> incomplete `seen`
                    continue
                lat, lon = coords
                if not region.contains(lat, lon, margin=0.1):
                    continue
                yield NormalizedRecord(
                    source_ref=name,
                    name=name,
                    org_type="drop_bin",
                    org_name="Wearable Collections",
                    lat=lat,
                    lon=lon,
                    accepted_items=["clothing", "shoes", "textiles"],
                )


def main():
    from .. import db
    from ..regions import get_region
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    try:
        load(WearableCollectionsScraper(), get_region(), conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
