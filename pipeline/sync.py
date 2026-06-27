"""Lean scheduled re-sync: re-ingest ingest sources (which advances freshness AND runs
closure detection), then dedup + promote. No demo prints. Run by the `scheduler` service
(docker compose --profile scheduler up) or host cron: `python -m pipeline.sync`."""
from __future__ import annotations

import logging

from . import db, dedup, promote
from .osm_ingest import OsmScraper
from .regions import get_region
from .scrapers.base import load
from .scrapers.planet_aid import PlanetAidScraper
from .scrapers.salvation_army import SalvationArmyScraper

log = logging.getLogger("opendrop.sync")


def main():
    logging.basicConfig(level=logging.INFO)
    region = get_region()
    conn = db.connect()
    try:
        for scraper in (OsmScraper(), SalvationArmyScraper(), PlanetAidScraper()):
            try:
                load(scraper, region, conn)
            except Exception as e:  # noqa: BLE001
                log.warning("sync %s failed: %s", scraper.code, e)
        dedup.run(conn)
        promote.run(conn)
        log.info("sync complete for region %s", region.name)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
