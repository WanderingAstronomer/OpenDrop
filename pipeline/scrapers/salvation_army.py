"""The Salvation Army — first-party donation locations (INGEST).
API: GET https://satruck.org/apiservices/pickup/donategoods/locations?Type=3&ZipCode=NNNNN&otid=0
Sweep Columbus-area ZIPs, dedupe on LocationGUID. (FINDINGS Finding 2.)"""
from __future__ import annotations

import logging

from .base import BaseScraper, NormalizedRecord, load
from .http import PoliteClient

log = logging.getLogger("opendrop.salvation_army")

API = "https://satruck.org/apiservices/pickup/donategoods/locations"


def _org_type(type_name: str | None) -> str:
    tn = (type_name or "").upper()
    if "STORE" in tn:
        return "charity_store"
    return "donation_center"


class SalvationArmyScraper(BaseScraper):
    code = "salvation_army"

    def fetch(self, region):
        seen: set[str] = set()
        with PoliteClient(timeout=30, headers={"User-Agent": "Mozilla/5.0 (OpenDrop civic open-data)"}) as client:
            for zip_code in (region.zips or []):
                try:
                    r = client.get(API, params={"Type": 3, "ZipCode": zip_code, "otid": 0})
                    r.raise_for_status()
                    payload = r.json()
                except Exception as e:  # noqa: BLE001
                    self.fetch_failures += 1  # swallowed ZIP call -> `seen` incomplete -> no reconcile
                    log.warning("satruck %s failed: %s", zip_code, e)
                    continue
                locs = (payload.get("RetVal") or {}).get("Locations") or []
                for loc in locs:
                    guid = loc.get("LocationGUID") or str(loc.get("Id") or "")
                    if not guid or guid in seen:
                        continue
                    seen.add(guid)
                    lat, lon = loc.get("Latitude"), loc.get("Longitude")
                    if lat is None or lon is None:
                        continue
                    lat, lon = float(lat), float(lon)
                    # A ZIP near a region edge returns locations that physically belong to the
                    # neighbouring region; keep only those inside this region (small margin for
                    # border ZIPs), matching the USAgain/Wearable sibling scrapers. Without this an
                    # out-of-region store would be loaded against the wrong region's run.
                    if not region.contains(lat, lon, margin=0.05):
                        continue
                    addr = " ".join(x for x in (loc.get("Address1"), loc.get("Address2")) if x) or None
                    yield NormalizedRecord(
                        source_ref=guid,
                        name=loc.get("Name") or "The Salvation Army",
                        org_type=_org_type(loc.get("TypeName")),
                        org_name="The Salvation Army",
                        lat=lat,
                        lon=lon,
                        address_line=addr,
                        city=loc.get("City"),
                        state=(loc.get("State") or None),
                        postal_code=loc.get("Zip"),
                        hours_raw=loc.get("Hours"),
                        phone=loc.get("ContactPhone"),
                        website=loc.get("Website"),
                    )


def main():
    from .. import db
    from ..regions import get_region
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    try:
        load(SalvationArmyScraper(), get_region(), conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
