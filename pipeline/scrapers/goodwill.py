"""Goodwill — ENRICH-ONLY scraper-interface PATTERN DEMO (decision D1).

Goodwill's ToS forbids storing/reproducing its data, so this scraper runs the full
fetch -> normalize -> dedup-match path but the loader persists NOTHING (sources.storage_policy
= 'enrich_only'): it only writes a scrape_log row with the enrich-match count. This proves the
BaseScraper pattern on a second real org without violating "all stored data must be redistributable".

Endpoint (FINDINGS Finding 2 / Dedup):
  nonce harvested from https://www.goodwill.org/locator/  (window.gwlfGlobal nonce, param 'security')
  GET https://www.goodwill.org/wp-admin/admin-ajax.php?action=gwlf_get_locations&security=<nonce>
      &lat=&lng=&radius=&cats=1   (cats=1 => Donation Site)
"""
from __future__ import annotations

import logging
import re

from .base import BaseScraper, NormalizedRecord, load
from .http import PoliteClient

log = logging.getLogger("opendrop.goodwill")

LOCATOR = "https://www.goodwill.org/locator/"
AJAX = "https://www.goodwill.org/wp-admin/admin-ajax.php"
_NONCE_RE = re.compile(r'nonce["\']?\s*[:=]\s*["\']([0-9a-f]{8,})["\']', re.I)


class GoodwillScraper(BaseScraper):
    code = "goodwill"

    def fetch(self, region):
        with PoliteClient(timeout=30, headers={"User-Agent": "Mozilla/5.0 (OpenDrop civic open-data)"}) as client:
            nonce = self._nonce(client)
            if not nonce:
                log.warning("goodwill: could not harvest nonce; no records")
                return
            lat, lng = region.center
            try:
                r = client.get(AJAX, params={
                    "action": "gwlf_get_locations", "security": nonce,
                    "lat": lat, "lng": lng, "radius": region.radius_mi, "cats": 1,
                })
                r.raise_for_status()
                body = r.json()
            except Exception as e:  # noqa: BLE001
                log.warning("goodwill ajax failed: %s", e)
                return
            # Goodwill's AJAX wraps the rows differently across deployments: sometimes
            # body["data"]["data"] (nested), sometimes a flat body["data"] list. Unwrap both
            # without assuming a shape — calling .get() on a non-empty flat list used to raise
            # AttributeError straight out of fetch() (the unwrap sits outside the try/except).
            raw = body.get("data")
            if isinstance(raw, dict):
                data = raw.get("data") or []
            elif isinstance(raw, list):
                data = raw
            else:
                data = []
            for loc in data:
                if not isinstance(loc, dict):
                    continue
                lat_, lng_ = loc.get("LocationLatitude1"), loc.get("LocationLongitude1")
                if lat_ in (None, "") or lng_ in (None, ""):
                    continue
                # donation sites only
                if str(loc.get("ci_servD") or "0") not in ("1", "true", "True"):
                    if "Donation" not in str(loc.get("calcd_ServicesOffered") or ""):
                        continue
                yield NormalizedRecord(
                    source_ref=str(loc.get("LocationId") or loc.get("LocationName")),
                    name=loc.get("LocationName") or "Goodwill Donation Center",
                    org_type="donation_center",
                    org_name="Goodwill",
                    lat=float(lat_),
                    lon=float(lng_),
                    address_line=loc.get("LocationStreetAddress1"),
                    city=loc.get("LocationCity1"),
                    state=(loc.get("LocationState1") or None),
                    postal_code=loc.get("LocationPostal1"),
                    phone=loc.get("LocationPhoneOffice"),
                )

    @staticmethod
    def _nonce(client: PoliteClient) -> str | None:
        try:
            html = client.get(LOCATOR).text
        except Exception as e:  # noqa: BLE001
            log.warning("goodwill locator fetch failed: %s", e)
            return None
        m = _NONCE_RE.search(html)
        return m.group(1) if m else None


def main():
    from .. import db
    from ..regions import get_region
    logging.basicConfig(level=logging.INFO)
    conn = db.connect()
    try:
        load(GoodwillScraper(), get_region(), conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
