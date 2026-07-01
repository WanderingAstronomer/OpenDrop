"""Region definitions — coverage is data, not code.

Two tiers live here:

* **Curated regions** (`columbus`, `ohio`, `greater_ohio`) — hand-tuned metro/multi-state areas
  kept for backward compatibility and as the friendly default.
* **National regions** — built lazily from the vendored ZIP table (`data/us_zips.csv`): one
  `Region` per state (bbox + center **derived** from that state's ZIP coordinates) plus a single
  synthesized `usa` region spanning them all. Adding/adjusting national coverage is therefore a
  data change (regenerate the CSV), not a code change.

Each scraper consumes the fields it needs: `bbox` for OSM/grid sources, `zips` for ZIP-sweeps,
`center` + `radius_mi` for point-radius locators. The national seeder iterates the per-state
regions so OSM is naturally tiled and reconciliation is naturally scoped (see
`pipeline/seed_national.py`)."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data" / "us_zips.csv"

COLUMBUS_ZIPS = [
    "43215", "43004", "43016", "43017", "43026", "43054", "43068", "43081",
    "43123", "43147", "43204", "43209", "43214", "43219", "43229", "43230", "43235",
]

# Representative ZIPs across Ohio metros (ZIP-sweep scrapers dedupe overlaps on LocationGUID).
OHIO_ZIPS = [
    # Columbus
    "43215", "43004", "43017", "43229", "43230", "43123", "43055", "43130",
    # Cleveland / NE Ohio
    "44113", "44102", "44120", "44130", "44144", "44035", "44052", "44129", "44221",
    # Cincinnati / SW Ohio
    "45202", "45211", "45227", "45238", "45240", "45011", "45044",
    # Toledo / NW Ohio
    "43604", "43615", "43623", "43402", "45840", "44870",
    # Akron / Canton
    "44303", "44310", "44319", "44708", "44720", "44691",
    # Dayton / Springfield
    "45402", "45414", "45424", "45459", "45429", "45503",
    # Youngstown / Steubenville
    "44503", "44512", "43952",
    # Southern / smaller metros
    "45601", "45701", "45662", "45801", "44903", "43701", "43302",
]


@dataclass
class Region:
    name: str
    bbox: tuple           # (south, west, north, east)
    center: tuple         # (lat, lon)
    zips: list = field(default_factory=list)
    radius_mi: int = 25

    @property
    def bbox_str(self) -> str:
        s, w, n, e = self.bbox
        return f"{s},{w},{n},{e}"

    def contains(self, lat: float, lon: float, margin: float = 0.0) -> bool:
        s, w, n, e = self.bbox
        return (s - margin) <= lat <= (n + margin) and (w - margin) <= lon <= (e + margin)


# "Greater Ohio": Ohio + every bordering state (MI, IN, KY, WV, PA). Multi-state proof that
# coverage is data, not code — one Region entry, no scraper changes. ZIP-sweep sources
# (Salvation Army, USAgain) cover a metro ONLY if it has a ZIP here, AND the bbox must contain
# that metro or contains()-filtering would drop the very bins the sweep fetched — so every metro
# below sits inside the bbox. (USAgain has no Ohio coverage but DOES serve MI/IN/PA, so the
# neighbors add genuinely new bins.)
GREATER_OHIO_ZIPS = OHIO_ZIPS + [
    # Michigan — Detroit, Dearborn, Taylor, Ann Arbor, Flint, Lansing, Grand Rapids
    "48201", "48226", "48127", "48180", "48104", "48503", "48906", "48933", "49503", "49546",
    # Indiana — Indianapolis, Fort Wayne, South Bend, Gary, Evansville
    "46204", "46225", "46802", "46601", "46402", "47708",
    # Kentucky — Louisville, Lexington, Covington, Bowling Green
    "40202", "40299", "40508", "41011", "42101",
    # West Virginia — Charleston, Huntington, Morgantown, Wheeling, Parkersburg
    "25301", "25701", "26505", "26003", "26101",
    # Pennsylvania — Pittsburgh, Erie, Greensburg, Washington, Harrisburg, Allentown, Scranton, Philadelphia
    "15222", "15201", "16501", "15601", "15301", "17101", "18101", "18503", "19103", "19107",
]


def _columbus() -> Region:
    env = os.environ.get("SEED_REGION_BBOX")
    if env:
        s, w, n, e = (float(x) for x in env.split(","))
        bbox = (s, w, n, e)
    else:
        bbox = (39.80, -83.25, 40.18, -82.75)
    return Region("columbus", bbox, (39.96, -82.99), COLUMBUS_ZIPS, 25)


REGIONS = {
    "columbus": _columbus(),
    "ohio": Region("ohio", (38.40, -84.82, 41.98, -80.52), (40.0, -82.7), OHIO_ZIPS, 30),
    # bbox (south, west, north, east) holds every metro in GREATER_OHIO_ZIPS: S=Bowling Green KY
    # (36.99), W=Evansville/Gary IN (~-87.6), N=Flint/Grand Rapids MI (~43.0), E=Philadelphia PA
    # (~-75.1). center sits in eastern OH; wide radius_mi only widens Goodwill's enrich-only sweep.
    "greater_ohio": Region(
        "greater_ohio", (36.50, -88.20, 44.00, -74.70), (40.20, -81.50), GREATER_OHIO_ZIPS, 300
    ),
}


# --- Data-driven national regions (50 states + DC, synthesized from data/us_zips.csv) ----------

# State code -> friendly lowercase name, so `REGION=california` resolves to the `ca` region.
STATE_NAMES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california",
    "CO": "colorado", "CT": "connecticut", "DE": "delaware", "DC": "district of columbia",
    "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho", "IL": "illinois",
    "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york",
    "NC": "north carolina", "ND": "north dakota", "OH": "ohio_full", "OK": "oklahoma",
    "OR": "oregon", "PA": "pennsylvania", "RI": "rhode island", "SC": "south carolina",
    "SD": "south dakota", "TN": "tennessee", "TX": "texas", "UT": "utah", "VT": "vermont",
    "VA": "virginia", "WA": "washington", "WV": "west virginia", "WI": "wisconsin",
    "WY": "wyoming",
}
# Reverse map for friendly-name lookups. Note OH's friendly name is "ohio_full" so it never
# shadows the curated multi-metro `ohio` region above (which `get_region` resolves first anyway).
_NAME_TO_CODE = {name: code.lower() for code, name in STATE_NAMES.items()}

# Pad (degrees) added around the hull of a state's ZIP centroids. Donation locations cluster at
# population centers (≈ ZIP centroids), so a small pad covers where bins actually are without
# bloating Overpass queries. contains()-filtering still holds because the bbox is derived FROM the
# swept ZIPs.
_STATE_PAD = 0.20


def _load_zip_rows() -> list[tuple[str, str, float, float]]:
    """Parse the vendored ZIP table -> [(zip, state, lat, lon)]. Empty list if the file is absent
    or unreadable (graceful degradation — national regions simply won't be available)."""
    if not _DATA.exists():
        return []
    rows: list[tuple[str, str, float, float]] = []
    with _DATA.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        next(reader, None)  # header
        for row in reader:
            if len(row) < 4:
                continue
            try:
                lat, lon = float(row[2]), float(row[3])
            except ValueError:
                continue
            # Defensively reject anything outside the US envelope (placeholder/null-island coords)
            # so a stray bad row can't blow out a derived state bbox.
            if not (17.0 <= lat <= 72.0 and -180.0 <= lon <= -64.0):
                continue
            rows.append((row[0], row[1], lat, lon))
    return rows


def _bbox_center(points: list[tuple[float, float]], pad: float) -> tuple[tuple, tuple]:
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    bbox = (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)
    center = (sum(lats) / len(lats), sum(lons) / len(lons))
    return bbox, center


@lru_cache(maxsize=1)
def _national_regions() -> dict[str, Region]:
    """{name: Region} for every state (keyed by lowercase code, e.g. "ca") plus a synthesized
    "usa" region. Built once per process from the vendored ZIP table; {} if data is missing."""
    rows = _load_zip_rows()
    if not rows:
        return {}
    by_state: dict[str, list[tuple[str, float, float]]] = {}
    for z, st, lat, lon in rows:
        by_state.setdefault(st, []).append((z, lat, lon))

    regions: dict[str, Region] = {}
    all_points: list[tuple[float, float]] = []
    all_zips: list[str] = []
    for st, entries in sorted(by_state.items()):
        points = [(lat, lon) for _, lat, lon in entries]
        zips = [z for z, _, _ in entries]
        bbox, center = _bbox_center(points, _STATE_PAD)
        regions[st.lower()] = Region(st.lower(), bbox, center, zips, radius_mi=150)
        all_points.extend(points)
        all_zips.extend(zips)

    # National region: union bbox over every state, all ZIPs. radius_mi is only used by Goodwill's
    # enrich-only (persists-nothing) sweep, so an outsized value is harmless.
    bbox, center = _bbox_center(all_points, 0.0)
    regions["usa"] = Region("usa", bbox, center, all_zips, radius_mi=2500)
    return regions


def state_regions() -> list[Region]:
    """The 50 states + DC as data-driven Regions, sorted by code. Empty if the ZIP table is
    missing. The national seeder iterates these (per-state OSM tiling + reconciliation scoping)."""
    nat = _national_regions()
    return [nat[code] for code in sorted(nat) if code != "usa"]


def available_regions() -> list[str]:
    """All region names that get_region() will resolve (curated + per-state codes + usa)."""
    return sorted(set(REGIONS) | set(_national_regions()))


def get_region(name: str | None = None) -> Region:
    key = (name or os.environ.get("REGION") or "columbus").lower().strip()
    if key in REGIONS:                      # curated: columbus / ohio / greater_ohio
        return REGIONS[key]
    national = _national_regions()
    if key in national:                     # per-state code ("oh", "ca", ...) or "usa"
        return national[key]
    code = _NAME_TO_CODE.get(key)           # friendly full name ("california" -> "ca")
    if code and code in national:
        return national[code]
    return REGIONS["columbus"]              # unknown -> safe fallback
