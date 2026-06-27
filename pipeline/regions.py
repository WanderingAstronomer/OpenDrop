"""Region definitions — coverage is data, not code. Add a metro by adding an entry here.
Each scraper consumes the fields it needs (bbox for OSM/grid, zips for ZIP-sweeps,
center+radius for point-radius locators)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

COLUMBUS_ZIPS = [
    "43215", "43004", "43016", "43017", "43026", "43054", "43068", "43081",
    "43123", "43147", "43204", "43209", "43214", "43219", "43229", "43230", "43235",
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


def _columbus() -> Region:
    env = os.environ.get("SEED_REGION_BBOX")
    if env:
        s, w, n, e = (float(x) for x in env.split(","))
        bbox = (s, w, n, e)
    else:
        bbox = (39.80, -83.25, 40.18, -82.75)
    return Region("columbus", bbox, (39.96, -82.99), COLUMBUS_ZIPS, 25)


REGIONS = {"columbus": _columbus()}


def get_region(name: str | None = None) -> Region:
    key = (name or os.environ.get("REGION") or "columbus").lower()
    return REGIONS.get(key, REGIONS["columbus"])
