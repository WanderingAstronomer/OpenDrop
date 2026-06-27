"""Seed a region so the map has real data on first boot.
OSM (fixture-backed) + Salvation Army + Planet Aid (ingest) + Goodwill (enrich-only) + dedup + promote."""
from __future__ import annotations

import logging

from . import db, dedup, promote
from .osm_ingest import OsmScraper
from .regions import get_region
from .scrapers.base import load
from .scrapers.goodwill import GoodwillScraper
from .scrapers.planet_aid import PlanetAidScraper
from .scrapers.salvation_army import SalvationArmyScraper


def _try(scraper, region, conn):
    try:
        return load(scraper, region, conn)
    except Exception as e:  # noqa: BLE001 — live endpoints are optional for the demo
        print(f"  {scraper.code} failed (non-fatal): {e}")
        return {}


def main():
    logging.basicConfig(level=logging.INFO)
    region = get_region()
    conn = db.connect()
    try:
        print(f"== Region: {region.name} {region.bbox} ==")
        print("== OSM ingest ==")
        osm = load(OsmScraper(), region, conn)
        print("== Salvation Army (ingest) ==")
        sa = _try(SalvationArmyScraper(), region, conn)
        print("== Planet Aid (ingest) ==")
        pa = _try(PlanetAidScraper(), region, conn)
        print("== Goodwill (enrich-only; persists nothing) ==")
        gw = _try(GoodwillScraper(), region, conn)
        print("== Dedup ==")
        merges = dedup.run(conn)
        print("== Promote pending submissions ==")
        promoted = promote.run(conn)

        counts = conn.execute("SELECT status, count(*) AS n FROM locations GROUP BY status").fetchall()
        by_type = conn.execute(
            "SELECT org_type, count(*) AS n FROM locations WHERE status='active' GROUP BY org_type ORDER BY n DESC"
        ).fetchall()
        print("\n=== SEED SUMMARY ===")
        print("  osm        :", osm)
        print("  salv. army :", sa)
        print("  planet aid :", pa)
        print("  goodwill   :", gw, "(enrich-only — 0 stored)")
        print("  dedup merges:", merges, " promoted:", promoted)
        print("  locations by status:", {r["status"]: r["n"] for r in counts})
        print("  active by org_type :", {r["org_type"]: r["n"] for r in by_type})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
