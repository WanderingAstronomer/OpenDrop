"""Operator-only usage statistics — a private "how's it doing" snapshot.

GET /admin/stats returns read-only aggregate counts: dataset size + composition, geographic
coverage, community engagement, per-source freshness, and recent growth. It is gated by
`require_operator` (the `X-Operator-Token` header) and 404s — not 401/403 — without a valid token,
so the whole surface is invisible to probes, exactly like the moderation admin routes.

Privacy: this endpoint returns ONLY counts and timestamps. It never exposes IPs, ip_hashes, or any
per-actor identifier — the same privacy posture as the rest of the app.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends

from .. import db
from ..deps import require_operator

router = APIRouter()


@router.get("/admin/stats", dependencies=[Depends(require_operator)])
async def admin_stats():
    async with db.pool.connection() as conn:
        async def rows(sql):
            cur = await conn.execute(sql)
            return await cur.fetchall()

        async def row(sql):
            cur = await conn.execute(sql)
            return await cur.fetchone()

        # --- locations: total, public, status mix, org-type mix (active) --------------------------
        by_status = {r["k"]: r["n"] for r in await rows(
            "SELECT status::text AS k, count(*) AS n FROM locations GROUP BY status")}
        loc = await row(
            "SELECT count(*) AS total, "
            "count(*) FILTER (WHERE status = 'active' AND is_redistributable) AS public "
            "FROM locations")
        by_org = {r["k"]: r["n"] for r in await rows(
            "SELECT org_type AS k, count(*) AS n FROM locations WHERE status = 'active' "
            "GROUP BY org_type ORDER BY n DESC")}

        # --- sources: source-link counts -----------------------------------------------------------
        by_source = {r["k"]: r["n"] for r in await rows(
            "SELECT source_code AS k, count(*) AS n FROM location_sources "
            "GROUP BY source_code ORDER BY n DESC")}

        # --- geographic coverage (active only) -----------------------------------------------------
        cov = await row("SELECT count(DISTINCT state) AS states FROM locations "
                        "WHERE status = 'active' AND state IS NOT NULL")
        top_states = [{"state": r["state"], "count": r["n"]} for r in await rows(
            "SELECT state, count(*) AS n FROM locations WHERE status = 'active' AND state IS NOT NULL "
            "GROUP BY state ORDER BY n DESC, state LIMIT 10")]

        # --- community engagement ------------------------------------------------------------------
        votes = {r["k"]: r["n"] for r in await rows(
            "SELECT vote::text AS k, count(*) AS n FROM votes GROUP BY vote")}
        pin = await row("SELECT count(*) AS total, count(*) FILTER (WHERE applied) AS applied "
                        "FROM location_corrections")
        field = await row("SELECT count(*) AS total, count(*) FILTER (WHERE applied) AS applied "
                          "FROM field_corrections")
        photos = await row(
            "SELECT count(*) AS total, count(*) FILTER (WHERE removed_at IS NULL) AS visible, "
            "count(*) FILTER (WHERE removed_at IS NOT NULL) AS hidden FROM location_images")
        reports = await row(
            "SELECT count(*) FILTER (WHERE resolved_at IS NULL) AS open, "
            "count(*) FILTER (WHERE resolved_at IS NOT NULL) AS resolved FROM content_reports")
        attr_votes = await row("SELECT count(*) AS n FROM attribute_votes")
        subs = await row("SELECT count(*) AS total, "
                         "count(*) FILTER (WHERE promoted_location_id IS NOT NULL) AS promoted "
                         "FROM pending_locations")

        # --- freshness: last scrape per source + newest location -----------------------------------
        last_scrape = [
            {"source": r["source_code"],
             "finished_at": r["run_finished_at"].isoformat() if r["run_finished_at"] else None,
             "status": r["status"], "new": r["records_new"]}
            for r in await rows(
                "SELECT DISTINCT ON (source_code) source_code, run_finished_at, status, records_new "
                "FROM scrape_log ORDER BY source_code, run_started_at DESC")]
        newest = await row("SELECT max(created_at) AS ts FROM locations")

        # --- recent growth (7d / 30d) --------------------------------------------------------------
        def _win(alias):
            return (f"count(*) FILTER (WHERE created_at > now() - interval '7 days')  AS {alias}7, "
                    f"count(*) FILTER (WHERE created_at > now() - interval '30 days') AS {alias}30")

        rec = await row(f"SELECT {_win('l')} FROM locations")
        recp = await row(f"SELECT {_win('p')} FROM location_images")
        recv = await row(f"SELECT {_win('v')} FROM votes")
        recr = await row(f"SELECT {_win('r')} FROM content_reports")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "locations": {
            "total": loc["total"],
            "public": loc["public"],
            "by_status": by_status,
            "by_org_type": by_org,
        },
        "sources": {"links_by_source": by_source},
        "coverage": {"states_covered": cov["states"], "top_states": top_states},
        "community": {
            "votes": votes,
            "pin_corrections": {"total": pin["total"], "applied": pin["applied"]},
            "field_corrections": {"total": field["total"], "applied": field["applied"]},
            "attribute_votes": attr_votes["n"],
            "photos": {"total": photos["total"], "visible": photos["visible"], "hidden": photos["hidden"]},
            "reports": {"open": reports["open"], "resolved": reports["resolved"]},
            "pending_submissions": {"total": subs["total"], "promoted": subs["promoted"]},
        },
        "freshness": {
            "last_scrape": last_scrape,
            "newest_location_at": newest["ts"].isoformat() if newest["ts"] else None,
        },
        "recent": {
            "new_locations": {"d7": rec["l7"], "d30": rec["l30"]},
            "new_photos": {"d7": recp["p7"], "d30": recp["p30"]},
            "votes": {"d7": recv["v7"], "d30": recv["v30"]},
            "reports": {"d7": recr["r7"], "d30": recr["r30"]},
        },
    }
