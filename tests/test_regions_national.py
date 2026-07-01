"""Pure-Python tests for the data-driven national regions (no DB).

The 50 states + DC and the synthesized `usa` region are derived from the vendored ZIP table
(pipeline/data/us_zips.csv). These tests pin the contract the national seeder relies on:
every state resolves, its derived bbox actually contains its own ZIP centroids (so contains()
filtering doesn't throw away the bins a sweep just fetched), and friendly names / codes / `usa`
all resolve — while the curated `ohio` region is NOT shadowed by the per-state one.
Run: PYTHONPATH=. pytest tests/test_regions_national.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.regions import (  # noqa: E402
    REGIONS,
    available_regions,
    get_region,
    state_regions,
)

# A representative city per state-ish — each must land inside its state region's derived bbox.
CITY_IN_STATE = [
    ("ca", "Sacramento", 38.58, -121.49),
    ("ca", "San Diego", 32.72, -117.16),
    ("tx", "Austin", 30.27, -97.74),
    ("tx", "El Paso", 31.76, -106.49),
    ("ny", "Buffalo", 42.89, -78.88),
    ("fl", "Miami", 25.76, -80.19),
    ("wa", "Seattle", 47.61, -122.33),
    ("me", "Portland", 43.66, -70.26),
    ("ak", "Anchorage", 61.22, -149.90),
    ("hi", "Honolulu", 21.31, -157.86),
    ("dc", "Washington", 38.90, -77.04),
]


def test_state_regions_cover_all_jurisdictions():
    regs = state_regions()
    assert regs, "state regions must be available (is pipeline/data/us_zips.csv committed?)"
    assert len(regs) == 51, f"expected 50 states + DC, got {len(regs)}"
    codes = {r.name for r in regs}
    assert "usa" not in codes  # usa is the union region, not a 'state'
    assert {"ca", "tx", "ny", "dc", "oh", "ak", "hi"} <= codes


def test_every_state_centroid_sits_inside_its_own_bbox():
    # The bbox is derived FROM each state's ZIP coords (+pad), so the centroid must be interior.
    for r in state_regions():
        assert r.zips, f"{r.name} carries no ZIP sweep list"
        clat, clon = r.center
        assert r.contains(clat, clon), f"{r.name} centroid {r.center} fell outside its bbox {r.bbox}"


def test_representative_cities_land_in_their_state():
    for code, city, lat, lon in CITY_IN_STATE:
        r = get_region(code)
        assert r.name == code
        assert r.contains(lat, lon), f"{city} ({lat},{lon}) should be inside {code} bbox {r.bbox}"


def test_usa_region_spans_the_continent():
    usa = get_region("usa")
    assert usa.name == "usa"
    s, w, n, e = usa.bbox
    # Must reach from the SE (Florida/PR-free lower bound) up past the northern border and from the
    # Pacific to the Atlantic — and contain every state centroid.
    assert s < 30 and n > 45 and w < -120 and e > -70
    for r in state_regions():
        assert usa.contains(*r.center), f"usa bbox should contain {r.name} centroid {r.center}"


def test_friendly_names_and_codes_resolve():
    assert get_region("california").name == "ca"
    assert get_region("CALIFORNIA").name == "ca"          # case-insensitive
    assert get_region("new york").name == "ny"
    assert get_region("tx").name == "tx"
    assert get_region("usa").name == "usa"


def test_curated_ohio_is_not_shadowed_by_per_state_ohio():
    # 'ohio' is the curated multi-metro region; the per-state region is keyed 'oh'. Its friendly
    # name is deliberately 'ohio_full' so it can never collide with the curated 'ohio'.
    assert get_region("ohio") is REGIONS["ohio"]
    assert get_region("oh").name == "oh"
    assert get_region("ohio_full").name == "oh"
    assert get_region("oh") is not REGIONS["ohio"]


def test_unknown_region_falls_back_to_columbus():
    assert get_region("atlantis").name == "columbus"
    assert get_region("").name == "columbus"


def test_available_regions_lists_curated_and_national():
    avail = set(available_regions())
    assert {"columbus", "ohio", "greater_ohio", "usa", "ca", "dc"} <= avail


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(tests)} national-region tests passed.")
