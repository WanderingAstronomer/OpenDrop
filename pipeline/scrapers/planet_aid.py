"""Planet Aid — first-party yellow donation bins (INGEST). FINDINGS Finding 2.
API: GET https://api.binlocator.planetaid.org/AzureSearch/sites?latitude=&longitude=
returns the ~20 NEAREST sites to a point, regardless of distance.

Coverage is an ADAPTIVE QUADTREE, not a flat grid. The old flat sweep silently under-covered dense
metros: a nearest-N feed returns only the 20 closest sites to each grid point, so wherever >20 real
bins fall inside one coarse cell the surplus were never re-queried and vanished from the map. The
fix exploits the one guarantee a nearest-N API gives us — a query at P exhaustively enumerates every
bin within radius d_max(P) = the distance to the FARTHEST returned site (anything nearer than the
20th would have displaced it). A center-anchored square cell of half-side h is fully covered iff that
disk reaches its corner, i.e. d_max >= SAFETY * corner_m. When a full-cap cell fails that test it is
split into four quadrants and re-queried; sparse cells (fewer than N returned, or a wide d_max) never
split, so the sweep stays as cheap as the old grid everywhere except genuine density. Termination is
guaranteed by a size floor + depth cap + a per-region query budget; the co-located-cluster degenerate
(20+ bins within meters) stops at the floor and keeps the API's irreducible nearest-20 there.
"""
from __future__ import annotations

import logging
import os
import re

from ..common import haversine_m
from .base import BaseScraper, NormalizedRecord, load
from .http import PoliteClient

log = logging.getLogger("opendrop.planet_aid")

API = "https://api.binlocator.planetaid.org/AzureSearch/sites"
# siteAddress is a single combined string like "6501 Ducketts Ln  Elkridge,MD 21075".
# The comma reliably separates the locality (street + city) from STATE ZIP; anchor on it.
# (The previous non-greedy `(.*?)\s+([A-Za-z .'-]+),` collapsed the street down to just the
# house number — "500 Oak Ave Columbus" parsed as street="500", city="Oak Ave Columbus".)
_ADDR = re.compile(r"^(?P<locality>.+?),\s*(?P<state>[A-Z]{2})\s*(?P<postal>\d{5})")

# --- adaptive-sweep tunables ---------------------------------------------------------------------
# N_CAP: the upstream nearest-N cap. A cell that returns FEWER than this proves the API exhausted the
#   local feed (nothing hidden -> complete, never split); returning exactly N signals possible
#   truncation. This len<N short-circuit is also the exact backward-compat guard: every existing
#   test feeds <20 sites, so no cell ever subdivides and today's single-pass behavior is unchanged.
N_CAP = 20
# SAFETY: subdivide unless d_max >= SAFETY * corner_m. 10% dominates the ~0.1% NE-vs-SE corner
#   asymmetry, haversine-vs-geodesic error, and coarsely-rounded API geoPoints, so the covering disk
#   STRICTLY contains the convex cell and no in-region bin is silently dropped.
SAFETY = 1.10
# MIN_CELL_HALF_DEG: floor on a child's half-side in degrees (~278 m; cell side ~556 m). Below this
#   we accept the API's inherent nearest-N residual (a mall packing >20 bins in one floor cell).
MIN_CELL_HALF_DEG = 0.0025
# MAX_DEPTH: hard cap on subdivision depth below the seed grid — a second, independent monotone
#   barrier so neither float drift in the size test nor an off-by-one in depth can defeat termination.
MAX_DEPTH = 6
# Per-region query budget (env-overridable). On exhaustion the sweep stops subdividing, drains the
# remaining worklist as coarse leaves, and WARNs — so a pathological data error can't blow the
# national wall-clock budget. 6000 * ~0.5 s PoliteClient pace ~= 50 min worst-case for one region.
_DEFAULT_MAX_QUERIES = 6000


def _max_queries() -> int:
    raw = os.environ.get("PLANET_AID_MAX_QUERIES")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            log.warning("planet_aid: bad PLANET_AID_MAX_QUERIES=%r; using default %d", raw, _DEFAULT_MAX_QUERIES)
    return _DEFAULT_MAX_QUERIES


def _seed_cells(bbox):
    """Center-anchored, edge-covering top-level tiling, seeded from the UNCHANGED coarse step so the
    baseline (sparse/national) call count is byte-for-byte identical to the old flat grid. Yields
    (center_lat, center_lon, half_side, depth=0). The old grid anchored queries at each cell's SW
    CORNER, which left points up to a full cell — and up to h past the north/east bbox edge —
    uncovered; center-anchoring makes the worst-case gap exactly the half-diagonal to the corner."""
    south, west, north, east = bbox
    span = max(north - south, east - west)
    step0 = max(0.13, span / 18.0)      # identical to the old adaptive step -> baseline preserved
    h0 = step0 / 2.0
    lat = south + h0
    while lat < north + h0:              # '+ h0' slack guarantees a cell covering the north edge
        lon = west + h0
        while lon < east + h0:           # '+ h0' slack guarantees a cell covering the east edge
            yield round(lat, 6), round(lon, 6), h0, 0
            lon += step0
        lat += step0


def _corner_m(clat: float, clon: float, h: float) -> float:
    """Exact metric center-to-corner distance. Calling haversine to the real corner bakes in cos(lat)
    longitude foreshortening (correct in AK and FL alike) — no hand-rolled degrees->meters constant."""
    return haversine_m(clat, clon, clat + h, clon + h)


class PlanetAidScraper(BaseScraper):
    code = "planet_aid"

    def fetch(self, region):
        seen: set[str] = set()
        stack = list(_seed_cells(region.bbox))   # LIFO worklist; no recursion (budget is loop-audited)
        budget = _max_queries()
        queries = 0
        with PoliteClient(timeout=20, headers={"User-Agent": "OpenDrop/0.1 (civic open-data)"}) as client:
            while stack:
                if queries >= budget:
                    log.warning("planet_aid: query budget %d hit for region=%s; %d cell(s) left unswept",
                                budget, getattr(region, "name", "?"), len(stack))
                    break
                clat, clon, h, depth = stack.pop()
                queries += 1
                try:
                    r = client.get(API, params={"latitude": clat, "longitude": clon})
                    r.raise_for_status()
                    data = r.json() or []
                except Exception as e:  # noqa: BLE001
                    self.fetch_failures += 1  # swallowed cell -> `seen` incomplete -> loader skips reconcile
                    log.warning("planet_aid cell (%s,%s h=%.4f) failed: %s", clat, clon, h, e)
                    continue                  # do NOT subdivide a failed cell (no d_max; would 4x the failures)

                # One pass: accumulate d_max over ALL returned sites (in- OR out-of-region — nearest-N
                # is global, so an out-of-region site legitimately enlarges the proven-covered disk),
                # and yield the in-region, unseen ones. Emit on EVERY cell via the shared `seen` set;
                # a bin's survival never depends on a child re-querying it.
                d_max = 0.0
                for site in data:
                    gp = site.get("geoPoint") or {}
                    glat, glon = gp.get("latitude"), gp.get("longitude")
                    if glat is None or glon is None:
                        continue
                    try:
                        glat, glon = float(glat), float(glon)
                    except (TypeError, ValueError):
                        continue  # present-but-non-numeric coord: skip the dirty row, don't abort the sweep
                    d = haversine_m(clat, clon, glat, glon)
                    if d > d_max:
                        d_max = d
                    sid = str(site.get("id") or "")
                    if not sid or sid in seen:
                        continue
                    if not region.contains(glat, glon, margin=0.05):
                        continue  # the API returns nearest-N regardless of distance; keep only in-region
                    seen.add(sid)
                    yield self._to_record(site, sid, glat, glon)

                # Subdivide iff the cell hit the cap AND its covering disk fails to reach the corner —
                # and it is structurally allowed to split. can_split is PURELY geometric (never a
                # function of d_max), so the co-located cluster (d_max ~ 0 forever) still terminates
                # at the floor. Reaching the floor is a fetch SUCCESS and must not bump fetch_failures.
                saturated = len(data) >= N_CAP and d_max < SAFETY * _corner_m(clat, clon, h)
                can_split = depth < MAX_DEPTH and (h / 2.0) >= MIN_CELL_HALF_DEG and queries < budget
                if saturated and can_split:
                    q = h / 2.0
                    for dlat in (-q, q):
                        for dlon in (-q, q):
                            ccl, ccn = round(clat + dlat, 6), round(clon + dlon, 6)
                            if region.contains(ccl, ccn, margin=0.05):  # skip children fully outside region
                                stack.append((ccl, ccn, q, depth + 1))

    def _to_record(self, site, sid: str, glat: float, glon: float) -> NormalizedRecord:
        addr = (site.get("siteAddress") or "").strip()
        street = city = state = postal = None
        m = _ADDR.match(addr)
        if m:
            state, postal = m.group("state"), m.group("postal")
            locality = m.group("locality").strip()
            # locality is "<street> <city>" with no separator; the feed delimits the two with a
            # DOUBLE space ("6501 Ducketts Ln  Elkridge"). Prefer that split; otherwise treat the
            # trailing token as the city ("500 Oak Ave Columbus").
            if "  " in locality:
                street, _, city = locality.partition("  ")
            else:
                street, _, city = locality.rpartition(" ")
            street, city = street.strip(), city.strip()
            if not street:  # single-token locality -> it is the street, city unknown
                street, city = city, None
            street = street or None
            city = city or None
        elif addr:
            street = addr
        type_id = str(site.get("siteTypeId") or "")
        org_type = "donation_center" if type_id in ("20", "21") else "drop_bin"
        return NormalizedRecord(
            source_ref=sid,
            name=site.get("siteName") or "Planet Aid donation bin",
            org_type=org_type,
            org_name="Planet Aid",
            lat=glat,
            lon=glon,
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
