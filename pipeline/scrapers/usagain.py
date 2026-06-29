"""USAgain — green clothing-donation bins (INGEST). FINDINGS Finding 2.
Server-rendered HTML: GET https://usagain.com/find-treemachine?zip=NNNNN returns the 10
nearest bins as a results table plus Google-Maps marker blocks with the lat/lon. 15 states
(NO Ohio). All bins are unattended 24/7 drop-off. Dedupe on lat/lon."""
from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser

from .base import BaseScraper, NormalizedRecord, load
from .http import PoliteClient

log = logging.getLogger("opendrop.usagain")

URL = "https://usagain.com/find-treemachine"
_LATLNG = re.compile(r"new\s+google\.maps\.LatLng\(\s*([-\d.]+)\s*,\s*([-\d.]+)\s*\)")


def _parse(html: str):
    coords = [(float(a), float(b)) for a, b in _LATLNG.findall(html)]
    if len(coords) <= 1:
        return  # only the search-center marker (or none) => no bins
    bins = coords[1:]  # first LatLng is the search center

    tree = HTMLParser(html)
    rows = []
    for tr in tree.css("table.table tr"):
        cells = [c.text(strip=True) for c in tr.css("td")]
        if len(cells) >= 2:
            rows.append(cells)

    for i, (lat, lon) in enumerate(bins):
        name = address = None
        if i < len(rows):
            name, address = rows[i][0] or None, rows[i][1] or None
        yield NormalizedRecord(
            source_ref=f"{lat:.5f},{lon:.5f}",
            name=name or "USAgain donation bin",
            org_type="drop_bin",
            org_name="USAgain",
            lat=lat,
            lon=lon,
            address_line=address,
            accepted_items=["clothing", "shoes"],
            hours={"always": True},
        )


class UsAgainScraper(BaseScraper):
    code = "usagain"

    def fetch(self, region):
        seen: set[tuple] = set()
        with PoliteClient(timeout=25, headers={"User-Agent": "Mozilla/5.0 (OpenDrop civic open-data)"}) as client:
            for zip_code in (region.zips or []):
                try:
                    html = client.get(URL, params={"zip": zip_code}).text
                except Exception as e:  # noqa: BLE001
                    log.warning("usagain %s failed: %s", zip_code, e)
                    continue
                for rec in _parse(html):
                    key = (round(rec.lat, 5), round(rec.lon, 5))
                    if key in seen or not region.contains(rec.lat, rec.lon, margin=0.05):
                        continue
                    seen.add(key)
                    yield rec


def main():
    from .. import db
    from ..regions import get_region
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    try:
        load(UsAgainScraper(), get_region(), conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
