"""Gentle, resumable, overnight-scale national seeder.

Walks the 50 states + DC (data-driven regions from ``data/us_zips.csv``), running the ingest
scrapers once per state, then a final global dedup + promote. Built for ONE deliberate overnight
run against third-party APIs:

* **Gentle** — every scraper paces and backs off (see ``pipeline/scrapers/http.py``). Tune the
  global spacing with ``SCRAPER_REQUEST_DELAY_S``; wall-clock is roughly delay x request-count, so
  ~42k ZIPs x the ZIP-sweep sources lands in the overnight range at the 0.5 s default. Slow it
  further any time by raising the delay.
* **Resumable** — each state's completion is checkpointed in the ``seed_progress`` table
  (migration 0008). Re-running skips states already marked ``done``, so an interrupted run (Ctrl-C,
  container restart, a crash at state #37) picks up where it left off instead of re-sweeping. A
  state only flips to ``done`` after its full scraper set finishes, so a mid-state interruption
  re-runs that state cleanly.
* **Naturally tiled & scoped** — per-state iteration means OSM is queried per state bbox (tiled
  further inside ``osm_ingest``) and closure-detection reconciles within each state's bbox only,
  never across the whole country.

Run it:

    docker compose run --rm api python -m pipeline.seed_national      # or: bash scripts/seed_national.sh

Resume after an interruption: just run it again. Re-run everything from scratch: ``SEED_FORCE=1``
(ignores checkpoints), or ``TRUNCATE seed_progress`` to forget progress without re-importing.
"""
from __future__ import annotations

import logging
import os

from psycopg.types.json import Json

from . import db, dedup, promote
from .osm_ingest import OsmScraper
from .regions import state_regions
from .scrapers.base import load
from .scrapers.planet_aid import PlanetAidScraper
from .scrapers.salvation_army import SalvationArmyScraper
from .scrapers.usagain import UsAgainScraper
from .scrapers.wearable_collections import WearableCollectionsScraper

log = logging.getLogger("opendrop.seed_national")

_FINALIZE = "__finalize__"  # synthetic seed_progress row for the dedup/promote step


def _scrapers():
    # Goodwill is enrich-only (persists nothing) — pointless and load-heavy for a seed, so it is
    # excluded. OSM is per-state tiled; the three ZIP/grid sources sweep each state's ZIPs/bbox.
    return (OsmScraper(), SalvationArmyScraper(), PlanetAidScraper(),
            UsAgainScraper(), WearableCollectionsScraper())


def _ensure_progress_table(conn) -> None:
    """Defensive: create seed_progress if migration 0008 hasn't been applied (older deployments).
    Idempotent and identical to the migration, so it's a no-op once 0008 has run."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS seed_progress (
               region_name text PRIMARY KEY,
               status      text NOT NULL DEFAULT 'pending',
               started_at  timestamptz,
               finished_at timestamptz,
               detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
               updated_at  timestamptz NOT NULL DEFAULT now(),
               CONSTRAINT seed_progress_status_chk CHECK (status IN ('pending','running','done','failed'))
           )"""
    )
    conn.commit()


def _is_done(conn, name: str) -> bool:
    if os.environ.get("SEED_FORCE"):
        return False
    row = conn.execute("SELECT status FROM seed_progress WHERE region_name=%s", (name,)).fetchone()
    return bool(row and row["status"] == "done")


def _mark(conn, name: str, status: str, detail: dict | None = None,
          *, start: bool = False, finish: bool = False) -> None:
    conn.execute(
        """INSERT INTO seed_progress (region_name, status, detail, started_at, finished_at, updated_at)
           VALUES (%s, %s, %s, CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END, now())
           ON CONFLICT (region_name) DO UPDATE SET
             status      = EXCLUDED.status,
             detail      = COALESCE(EXCLUDED.detail, seed_progress.detail),
             started_at  = COALESCE(EXCLUDED.started_at, seed_progress.started_at),
             finished_at = COALESCE(EXCLUDED.finished_at, seed_progress.finished_at),
             updated_at  = now()""",
        (name, status, Json(detail or {}), start, finish),
    )
    conn.commit()


def _seed_state(conn, region) -> dict:
    """Run every ingest scraper for one state. Per-scraper isolation: a scraper that raises (after
    its own retries) is logged and skipped, not fatal to the state. KeyboardInterrupt is NOT caught,
    so Ctrl-C leaves the state 'running' (not 'done') and a resume re-runs it."""
    detail: dict = {}
    for scraper in _scrapers():
        try:
            detail[scraper.code] = load(scraper, region, conn)
        except Exception as e:  # noqa: BLE001 — live endpoints are best-effort
            log.warning("%s %s failed (non-fatal): %s", region.name, scraper.code, e)
            detail[scraper.code] = {"error": str(e)}
    return detail


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    regions = state_regions()
    conn = db.connect()
    try:
        _ensure_progress_table(conn)
        if not regions:
            log.warning("no state regions available (is pipeline/data/us_zips.csv present?); nothing to do")
            return

        total = len(regions)
        done_already = sum(1 for r in regions if _is_done(conn, r.name))
        log.info("national seed: %d states, %d already done, %d to go (delay=%ss)",
                 total, done_already, total - done_already,
                 os.environ.get("SCRAPER_REQUEST_DELAY_S", "0.5"))

        processed_any = False
        for i, region in enumerate(regions, 1):
            if _is_done(conn, region.name):
                log.info("[%d/%d] %s — already done, skipping", i, total, region.name)
                continue
            log.info("[%d/%d] %s — seeding (%d ZIPs, bbox %s)",
                     i, total, region.name, len(region.zips), tuple(round(x, 2) for x in region.bbox))
            _mark(conn, region.name, "running", start=True)
            detail = _seed_state(conn, region)
            _mark(conn, region.name, "done", detail, finish=True)
            processed_any = True

        # Final global dedup + promote. Idempotent, but the global dedup is a full scan, so only
        # re-run it when this invocation actually imported something (or finalize never completed).
        if processed_any or not _is_done(conn, _FINALIZE):
            log.info("finalize: dedup + promote")
            _mark(conn, _FINALIZE, "running", start=True)
            merges = dedup.run(conn)
            promoted = promote.run(conn)
            _mark(conn, _FINALIZE, "done", {"merges": merges, "promoted": promoted}, finish=True)
            log.info("finalize: %d merges, %d promoted", merges, promoted)
        else:
            log.info("finalize: already done and nothing new this run — skipping")

        counts = conn.execute("SELECT status, count(*) AS n FROM locations GROUP BY status").fetchall()
        log.info("national seed complete. locations by status: %s",
                 {r["status"]: r["n"] for r in counts})
    finally:
        conn.close()


if __name__ == "__main__":
    main()
