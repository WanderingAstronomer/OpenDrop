"""Seed the Ohio test region so the map has real data on first boot.
OSM ingest (fixture-backed) + Salvation Army (ingest) + Goodwill (enrich-only) + dedup + promote."""
from __future__ import annotations

import logging
import os

from . import db, dedup, promote
from .osm_ingest import DEFAULT_BBOX, OsmScraper
from .scrapers.base import load
from .scrapers.goodwill import GoodwillScraper
from .scrapers.salvation_army import SalvationArmyScraper


def main():
    logging.basicConfig(level=logging.INFO)
    region = os.environ.get("SEED_REGION_BBOX", DEFAULT_BBOX)
    conn = db.connect()
    try:
        print("== OSM ingest ==")
        osm = load(OsmScraper(), region, conn)

        print("== Salvation Army (ingest) ==")
        try:
            sa = load(SalvationArmyScraper(), None, conn)
        except Exception as e:  # noqa: BLE001 — live endpoint optional for the demo
            print("  Salvation Army scrape failed (non-fatal):", e)
            sa = {}

        print("== Goodwill (enrich-only; persists nothing) ==")
        try:
            gw = load(GoodwillScraper(), None, conn)
        except Exception as e:  # noqa: BLE001
            print("  Goodwill scrape failed (non-fatal):", e)
            gw = {}

        print("== Dedup ==")
        merges = dedup.run(conn)

        print("== Promote pending submissions ==")
        promoted = promote.run(conn)

        counts = conn.execute(
            "SELECT status, count(*) AS n FROM locations GROUP BY status"
        ).fetchall()
        by_type = conn.execute(
            "SELECT org_type, count(*) AS n FROM locations WHERE status='active' GROUP BY org_type ORDER BY n DESC"
        ).fetchall()
        print("\n=== SEED SUMMARY ===")
        print("  osm        :", osm)
        print("  salv. army :", sa)
        print("  goodwill   :", gw, "(enrich-only — 0 stored)")
        print("  dedup merges:", merges, " promoted:", promoted)
        print("  locations by status:", {r["status"]: r["n"] for r in counts})
        print("  active by org_type :", {r["org_type"]: r["n"] for r in by_type})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
