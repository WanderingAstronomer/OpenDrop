"""Scraper interface + shared loader. Every scraper yields NormalizedRecords; the loader
drives them all identically, honoring sources.storage_policy (ingest persists; enrich_only logs only)."""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from .. import dedup, store
from ..common import brand_key, normalize_house_number

log = logging.getLogger("opendrop.loader")


@dataclass
class NormalizedRecord:
    source_ref: str
    name: str
    org_type: str = "other"
    org_name: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    address_line: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    postal_code: Optional[str] = None
    hours: Optional[dict] = None
    hours_raw: Optional[str] = None
    accepted_items: Optional[list] = None
    phone: Optional[str] = None
    website: Optional[str] = None


class BaseScraper(ABC):
    code: str
    # Completeness signal for closure detection. A scraper MUST increment this for every
    # request it silently swallows (a failed Overpass tile, a failed ZIP/grid call) — because a
    # swallowed failure makes `seen` incomplete, and reconciling against an incomplete `seen`
    # falsely retires live locations. The loader resets it to 0 before each fetch and refuses to
    # reconcile when it ends > 0. Exhaustiveness (can this feed enumerate everything?) is a static
    # source property; fetch_failures answers the per-run question: did THIS fetch actually complete?
    fetch_failures: int = 0

    @abstractmethod
    def fetch(self, region) -> Iterable[NormalizedRecord]:
        ...


# Continental + AK/HI/PR-ish bounding box; rejects 0,0 and swapped lat/lon.
_US = (17.5, -180.0, 72.0, -64.0)  # (south, west, north, east)

# Closure-detection guardrails — three layers, learned from retiring ~633 real bins on live:
#
#   Layer 0 (static, the primary fix): only an EXHAUSTIVE source may reconcile. A nearest-N /
#     ZIP-radius / grid-sampled feed (Planet Aid, Salvation Army, USAgain) legitimately omits real
#     bins on a perfectly clean run, so "absent from this run" never means "closed". Gated on
#     sources.fetch_is_exhaustive (only OSM's Overpass bbox query is exhaustive).
#
#   Layer 1 (single-run breaker): even for an exhaustive source, a truncated/blocked response (the
#     API returned 3 of its usual 800 sites) makes almost every link look absent. Refuse if the run
#     saw too few records, or would retire more than a fraction of the source's in-region links.
#
#   Layer 2 (cumulative breaker): a series of runs each under Layer 1's threshold can still bleed a
#     region dry (Planet Aid eroded 643 links across 53 sub-40% runs). Sum executed retirements per
#     source+region over a rolling window and refuse once cumulative erosion crosses a fraction of
#     the region's high-water-mark link count. Recorded in reconcile_audit.
#
# All thresholds are env-overridable for a deliberate, verified operator re-sync.
_RECONCILE_MAX_FRACTION = float(os.environ.get("RECONCILE_MAX_FRACTION", "0.40"))
_RECONCILE_MIN_SEEN = int(os.environ.get("RECONCILE_MIN_SEEN", "5"))
_RECONCILE_CUMULATIVE_FRACTION = float(os.environ.get("RECONCILE_CUMULATIVE_FRACTION", "0.40"))
_RECONCILE_WINDOW = os.environ.get("RECONCILE_WINDOW", "180 days")
# Operator escape hatch: ignore the exhaustive gate for a deliberate, supervised full re-sync.
_RECONCILE_IGNORE_EXHAUSTIVE = os.environ.get("RECONCILE_IGNORE_EXHAUSTIVE") == "1"


def _in_us(lat, lon) -> bool:
    return lat is not None and lon is not None and _US[0] <= lat <= _US[2] and _US[1] <= lon <= _US[3]


# The 50 states + DC — the same universe as data/us_zips.csv (our data-driven region source). The DB
# CHECK only enforces the two-letter *format*, so an upstream feed can hand us a well-formed but bogus
# code (satruck.org returned "PE" for Pennsylvania ARCs). Reject by membership, not just shape, so a
# bad code becomes NULL (and the coord-based backfill can recover it) instead of a phantom "state".
US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH "
    "NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)


def _enrich(rec: NormalizedRecord) -> dict:
    d = asdict(rec)
    d["brand_key"] = brand_key(rec.org_name, rec.name)
    d["house_number"] = normalize_house_number(rec.address_line)
    s = (d.get("state") or "").strip().upper()
    d["state"] = s if s in US_STATES else None  # else NULL (bogus/foreign code -> let backfill recover)
    return d


def _match_dict(d: dict) -> dict:
    return {k: d.get(k) for k in ("lat", "lon", "name", "brand_key", "org_type", "house_number")}


def _record_audit(conn, code, region_key, baseline, would, retired, seen_n, executed, reason):
    """Append a row to reconcile_audit so the cumulative breaker has cross-run history and every
    skip/execution is observable. Best-effort: a missing table (pre-0011 DB) must never break a
    scrape, so we swallow and roll back on failure."""
    try:
        conn.execute(
            """INSERT INTO reconcile_audit
               (source_code, region_key, baseline, would_retire, retired, seen_count, executed, reason)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (code, region_key, baseline, would, retired, seen_n, executed, reason),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        log.warning("%s: reconcile_audit insert failed (continuing): %s", code, e)


def _reconcile(conn, src, region, seen) -> int:
    """Closure/deletion detection: drop this ingest source's links *within the region's bbox* that
    were NOT seen in this run (the source no longer lists them). Scoped to the region so a partial/
    regional sweep can't mass-delete other regions. The source-delete trigger then recomputes
    affected locations' confidence (a row with no ingest source falls below the floor).

    `src` is the sources row (store.get_source): code, storage_policy, fetch_is_exhaustive. Three
    guards (see module constants) protect against retiring real bins — the failure mode that retired
    ~633 live bins. Returns the number of links actually retired."""
    code = src["code"]
    if src["storage_policy"] != "ingest" or not seen or not hasattr(region, "bbox"):
        return 0
    region_key = getattr(region, "name", "?")

    # Layer 0 — exhaustive gate (the primary fix). A non-exhaustive feed's clean run legitimately
    # omits real bins, so it must NEVER reconcile. No audit row: this is a static config decision,
    # not erosion data, and a national run would otherwise spam one row per region per source.
    if not src.get("fetch_is_exhaustive") and not _RECONCILE_IGNORE_EXHAUSTIVE:
        log.info("%s: closure-detection OFF (source not exhaustive) — region=%s, seen=%d, no retirement",
                 code, region_key, len(seen))
        return 0

    s, w, n, e = region.bbox
    env = (w, s, e, n)  # ST_MakeEnvelope(xmin=lon_w, ymin=lat_s, xmax=lon_e, ymax=lat_n)

    current = conn.execute(
        """SELECT count(*) AS n FROM location_sources
           WHERE source_code = %s AND source_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)""",
        (code, *env),
    ).fetchone()["n"]
    if current == 0:
        return 0

    would = conn.execute(
        """SELECT count(*) AS n FROM location_sources
           WHERE source_code = %s
             AND source_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
             AND NOT (source_ref::text = ANY(%s))""",
        (code, *env, list(seen)),
    ).fetchone()["n"]
    if would == 0:
        return 0

    # Layer 1 — single-run breaker: truncated/blocked upstream response, not a wave of real closures.
    if len(seen) < _RECONCILE_MIN_SEEN or would > current * _RECONCILE_MAX_FRACTION:
        log.warning(
            "%s: SKIPPING closure-detection — run would retire %d of %d in-region link(s) "
            "(seen=%d, max_fraction=%.0f%%, min_seen=%d). Looks like a truncated/blocked upstream "
            "response, not real closures. Set RECONCILE_MAX_FRACTION / RECONCILE_MIN_SEEN to force.",
            code, would, current, len(seen), _RECONCILE_MAX_FRACTION * 100, _RECONCILE_MIN_SEEN,
        )
        _record_audit(conn, code, region_key, current, would, 0, len(seen), False, "single_run_breaker")
        return 0

    # Layer 2 — cumulative cross-run erosion breaker. Sum prior EXECUTED retirements for this
    # source+region over the window; the reference is the window's high-water-mark in-region link
    # count (largest recorded baseline, or current+prior which reconstructs the peak from now).
    # Refuse once cumulative erosion (prior + this run) would cross the fraction of that peak.
    hist = conn.execute(
        """SELECT COALESCE(SUM(retired), 0) AS prior, COALESCE(MAX(baseline), 0) AS hwm
             FROM reconcile_audit
            WHERE source_code = %s AND region_key = %s AND run_at >= now() - %s::interval""",
        (code, region_key, _RECONCILE_WINDOW),
    ).fetchone()
    prior, hwm = hist["prior"], max(hist["hwm"], current + hist["prior"])
    if hwm > 0 and (prior + would) > hwm * _RECONCILE_CUMULATIVE_FRACTION:
        log.warning(
            "%s: SKIPPING closure-detection — cumulative erosion guard: prior %d + would %d exceeds "
            "%.0f%% of region high-water-mark %d (region=%s). Slow multi-run bleed, not real closures. "
            "Set RECONCILE_CUMULATIVE_FRACTION to force.",
            code, prior, would, _RECONCILE_CUMULATIVE_FRACTION * 100, hwm, region_key,
        )
        _record_audit(conn, code, region_key, current, would, 0, len(seen), False, "cumulative_breaker")
        return 0

    rows = conn.execute(
        """DELETE FROM location_sources
           WHERE source_code = %s
             AND source_geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
             AND NOT (source_ref::text = ANY(%s))
           RETURNING location_id""",
        (code, *env, list(seen)),
    ).fetchall()
    conn.commit()
    _record_audit(conn, code, region_key, current, would, len(rows), len(seen), True, None)
    if rows:
        log.info("%s: reconciled %d of %d in-region location-link(s) as closed/absent (region=%s)",
                 code, len(rows), current, region_key)
    return len(rows)


def load(scraper: BaseScraper, region, conn) -> dict:
    src = store.get_source(conn, scraper.code)
    if src is None:
        raise ValueError(f"unknown source code: {scraper.code}")
    policy = src["storage_policy"]
    log_id = store.start_scrape_log(conn, scraper.code)
    fetched = upserted = new = enrich = skipped = rejected = errors = removed = 0
    seen: set[str] = set()
    scraper.fetch_failures = 0  # reset the per-run completeness signal (scrapers bump it on swallowed failures)
    try:
        for rec in scraper.fetch(region):
            fetched += 1
            if rec.lat is None or rec.lon is None:
                skipped += 1
                continue
            if not _in_us(rec.lat, rec.lon):
                rejected += 1  # data-quality gate: 0,0 / swapped / out-of-country coords
                continue
            d = _enrich(rec)
            md = _match_dict(d)

            if policy == "enrich_only":
                if dedup.find_match(conn, md) is not None:
                    enrich += 1
                continue  # persist NOTHING (D1)

            # Per-record isolation: one bad record (e.g. a constraint violation from dirty
            # upstream data) is skipped, not fatal to the whole source.
            try:
                existing = conn.execute(
                    "SELECT location_id FROM location_sources WHERE source_code=%s AND source_ref=%s",
                    (scraper.code, rec.source_ref),
                ).fetchone()
                if existing:
                    loc_id = existing["location_id"]
                    store.upsert_source(conn, loc_id, scraper.code, rec.source_ref, rec.lon, rec.lat, rec.name, d)
                    store.refresh_location_fields(conn, loc_id)
                    upserted += 1
                else:
                    match = dedup.find_match(conn, md)
                    if match is not None:
                        store.upsert_source(conn, match, scraper.code, rec.source_ref, rec.lon, rec.lat, rec.name, d)
                        store.refresh_location_fields(conn, match)
                        upserted += 1
                    else:
                        loc_id = store.insert_location(conn, d)
                        store.upsert_source(conn, loc_id, scraper.code, rec.source_ref, rec.lon, rec.lat, rec.name, d)
                        new += 1
                seen.add(rec.source_ref)
                conn.commit()
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                errors += 1
                log.warning("%s: skipped bad record %s: %s", scraper.code, rec.source_ref, e)

        # Only reconcile (closure-detect) on a PROVABLY COMPLETE run. Record errors OR swallowed
        # fetch failures (a dead Overpass tile / ZIP / grid call) both mean `seen` is incomplete, so
        # deleting "absent" links could falsely retire live locations. _reconcile then applies its
        # own exhaustive gate + single-run + cumulative breakers.
        fetch_failures = getattr(scraper, "fetch_failures", 0)
        if errors == 0 and fetch_failures == 0:
            removed = _reconcile(conn, src, region, seen)
        elif fetch_failures:
            log.warning("%s: skipping closure-detection (%d fetch failure(s) -> incomplete `seen`)",
                        scraper.code, fetch_failures)
        else:
            log.warning("%s: skipping closure-detection (%d record errors -> incomplete run)", scraper.code, errors)
        status = "partial" if (errors or rejected or fetch_failures) else "success"
        detail = {"skipped_no_coords": skipped, "rejected": rejected, "errors": errors,
                  "fetch_failures": fetch_failures, "removed": removed}
        if policy == "enrich_only":
            detail["enrich_matches"] = enrich
        store.finish_scrape_log(conn, log_id, status, fetched, upserted, new, removed, detail)
        log.info("%s: fetched=%d new=%d upserted=%d enrich=%d skipped=%d rejected=%d errors=%d removed=%d",
                 scraper.code, fetched, new, upserted, enrich, skipped, rejected, errors, removed)
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        store.finish_scrape_log(conn, log_id, "failed", fetched, upserted, new, 0, error=str(e))
        raise
    return {"fetched": fetched, "new": new, "upserted": upserted, "enrich": enrich,
            "skipped": skipped, "rejected": rejected, "errors": errors, "removed": removed}
