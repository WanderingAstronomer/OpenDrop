"""Planet Aid — first-party yellow donation bins (INGEST). FINDINGS Finding 2.
API: GET https://api.binlocator.planetaid.org/AzureSearch/sites?latitude=&longitude=
returns the 20 nearest sites; sweep a grid over the region bbox, filter to in-region, dedupe on id."""
from __future__ import annotations

import logging
import re

import httpx

from .base import BaseScraper, NormalizedRecord, load

log = logging.getLogger("opendrop.planet_aid")

API = "https://api.binlocator.planetaid.org/AzureSearch/sites"
# siteAddress is a single combined string like "6501 Ducketts Ln  Elkridge,MD 21075"
_ADDR = re.compile(r"^(.*?)\s+([A-Za-z .'-]+),\s*([A-Z]{2})\s*(\d{5})")


def _grid(bbox, step=0.13):
    south, west, north, east = bbox
    lat = south
    while lat <= north:
        lon = west
        while lon <= east:
            yield round(lat, 4), round(lon, 4)
            lon += step
        lat += step


class PlanetAidScraper(BaseScraper):
    code = "planet_aid"

    def fetch(self, region):
        seen: set[str] = set()
        # adaptive grid: coarser for large regions so a statewide sweep isn't ~900 calls
        span = max(region.bbox[2] - region.bbox[0], region.bbox[3] - region.bbox[1])
        step = max(0.13, span / 18.0)
        with httpx.Client(timeout=20, headers={"User-Agent": "OpenDrop/0.1 (civic open-data)"}) as client:
            for lat, lon in _grid(region.bbox, step):
                try:
                    r = client.get(API, params={"latitude": lat, "longitude": lon})
                    r.raise_for_status()
                    data = r.json()
                except Exception as e:  # noqa: BLE001
                    log.warning("planet_aid grid (%s,%s) failed: %s", lat, lon, e)
                    continue
                for site in data or []:
                    sid = str(site.get("id") or "")
                    if not sid or sid in seen:
                        continue
                    gp = site.get("geoPoint") or {}
                    glat, glon = gp.get("latitude"), gp.get("longitude")
                    if glat is None or glon is None:
                        continue
                    if not region.contains(float(glat), float(glon), margin=0.05):
                        continue  # the API returns nearest-N regardless of distance; keep only in-region
                    seen.add(sid)
                    addr = (site.get("siteAddress") or "").strip()
                    street = city = state = postal = None
                    m = _ADDR.match(addr)
                    if m:
                        street, city, state, postal = m.group(1).strip(), m.group(2).strip(), m.group(3), m.group(4)
                    elif addr:
                        street = addr
                    type_id = str(site.get("siteTypeId") or "")
                    org_type = "donation_center" if type_id in ("20", "21") else "drop_bin"
                    yield NormalizedRecord(
                        source_ref=sid,
                        name=site.get("siteName") or "Planet Aid donation bin",
                        org_type=org_type,
                        org_name="Planet Aid",
                        lat=float(glat),
                        lon=float(glon),
                        address_line=street,
                        city=city,
                        state=state,
                        postal_code=postal,
                        accepted_items=["clothing", "shoes"],
                        hours={"always": True} if org_type == "drop_bin" else None,
                    )


def main():
    from .. import db
    from ..regions import get_region
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    try:
        load(PlanetAidScraper(), get_region(), conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
