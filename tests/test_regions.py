"""Pure-Python tests for region config (no DB). Focus: the multi-state `greater_ohio`
region must contain every metro it sweeps ZIPs for, reject clearly-outside points, and
unknown names must fall back to `columbus`.
Run: PYTHONPATH=. python tests/test_regions.py   (or pytest)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.regions import REGIONS, get_region  # noqa: E402

# (name, lat, lon) — the corner metros that define the multi-state bbox extremes, plus Columbus.
INSIDE = [
    ("Columbus OH", 39.96, -82.99),
    ("Detroit MI", 42.33, -83.05),
    ("Flint MI", 43.01, -83.69),        # northern extreme of the ZIP list
    ("Grand Rapids MI", 42.96, -85.67),
    ("Indianapolis IN", 39.77, -86.16),
    ("Evansville IN", 37.97, -87.57),   # near the SW corner
    ("Louisville KY", 38.25, -85.76),
    ("Bowling Green KY", 36.99, -86.44),  # southern extreme of the ZIP list
    ("Charleston WV", 38.35, -81.63),
    ("Pittsburgh PA", 40.44, -79.99),
    ("Philadelphia PA", 39.95, -75.16),   # eastern extreme of the ZIP list
]
OUTSIDE = [
    ("New York NY", 40.71, -74.00),   # just east of the -74.70 edge (keeps Wearable Collections out)
    ("Denver CO", 39.74, -104.99),
    ("Miami FL", 25.76, -80.19),
    ("Atlanta GA", 33.75, -84.39),    # south of 36.50
]


def test_greater_ohio_registered():
    assert "greater_ohio" in REGIONS
    r = REGIONS["greater_ohio"]
    assert r.name == "greater_ohio"
    assert r.zips, "region must carry a ZIP sweep list"


def test_greater_ohio_contains_every_swept_metro():
    r = REGIONS["greater_ohio"]
    for name, lat, lon in INSIDE:
        assert r.contains(lat, lon), f"{name} should be inside greater_ohio bbox"


def test_greater_ohio_excludes_outside_points():
    r = REGIONS["greater_ohio"]
    for name, lat, lon in OUTSIDE:
        assert not r.contains(lat, lon), f"{name} should be outside greater_ohio bbox"


def test_bbox_holds_all_zip_metros_for_contains_filtering():
    # ZIP-sweep sources filter fetched bins through contains(); if a swept metro fell outside the
    # bbox we'd fetch its bins then throw them away. The four corner metros prove the bbox covers
    # the full N/S/E/W span of the ZIP list (regression against the proposed-but-too-tight bbox).
    r = REGIONS["greater_ohio"]
    assert r.contains(43.01, -83.69)   # N: Flint
    assert r.contains(36.99, -86.44)   # S: Bowling Green
    assert r.contains(39.95, -75.16)   # E: Philadelphia
    assert r.contains(37.97, -87.57)   # W: Evansville


def test_unknown_region_falls_back_to_columbus():
    assert get_region("does-not-exist").name == "columbus"
    assert get_region("GREATER_OHIO").name == "greater_ohio"  # case-insensitive lookup


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(tests)} region tests passed.")
