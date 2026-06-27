"""Pure-Python tests for the dedup predicate + name/brand helpers (no DB needed).
Run: PYTHONPATH=. python tests/test_dedup_logic.py   (or pytest)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.common import brand_key, haversine_m, name_sim  # noqa: E402
from pipeline.dedup import is_match  # noqa: E402

BASE = (39.9600, -82.9900)


def rec(lat, lon, name, org_type="charity_store", house_number=None):
    return {"lat": lat, "lon": lon, "name": name, "brand_key": brand_key(name),
            "org_type": org_type, "house_number": house_number}


def test_brand_canonicalization():
    assert brand_key("Goodwill Columbus") == "goodwill"
    assert brand_key("The Salvation Army Family Store") == "salvation_army"
    assert brand_key("Volunteers of America Thrift") == "volunteers_of_america"
    assert brand_key("One More Time ETC.") is None
    assert brand_key("") is None


def test_name_sim_empty_trap():
    # The empty-string-name over-merge trap: two empties must NOT score 1.0.
    assert name_sim("", "") == 0.0
    assert name_sim("Goodwill", "Goodwill") >= 0.99


def test_haversine_sanity():
    d = haversine_m(39.96, -82.99, 39.98, -82.99)  # ~0.02 deg lat
    assert 2100 < d < 2300


def test_primary_match_same_brand_close():
    a = rec(*BASE, "Goodwill Columbus")
    b = rec(39.9620, -82.9900, "Goodwill Columbus")  # ~222 m
    assert is_match(a, b)


def test_no_match_different_brand_nearby():
    a = rec(*BASE, "Goodwill Columbus")
    voa = rec(39.9618, -82.9900, "Volunteers of America Thrift")  # ~200 m, different brand
    assert not is_match(a, voa)


def test_no_match_unbranded_thrift_near_goodwill():
    a = rec(*BASE, "Goodwill Columbus")
    other = rec(39.9621, -82.9900, "One More Time ETC.")  # ~233 m, brand None
    assert not is_match(a, other)


def test_tier2_house_number_recovers_far_pair():
    a = rec(*BASE, "Goodwill", house_number="123")
    far = rec(39.9646, -82.9900, "Goodwill", house_number="123")  # ~511 m
    assert haversine_m(a["lat"], a["lon"], far["lat"], far["lon"]) > 300  # primary fails
    assert is_match(a, far)  # tier-2 (<=600 + name_sim + same house number) catches it


def test_tier2_requires_house_number():
    a = rec(*BASE, "Goodwill", house_number=None)
    far = rec(39.9646, -82.9900, "Goodwill", house_number="123")
    assert not is_match(a, far)  # no house number on one side => tier-2 cannot fire


def test_unbranded_bins_colocated_merge():
    g = rec(*BASE, "Clothing donation bin", org_type="drop_bin")
    h = rec(39.96018, -82.9900, "Clothing donation bin", org_type="drop_bin")  # ~20 m
    assert is_match(g, h)


def test_unbranded_bins_apart_do_not_merge():
    g = rec(*BASE, "Clothing donation bin", org_type="drop_bin")
    i = rec(39.9609, -82.9900, "Clothing donation bin", org_type="drop_bin")  # ~100 m
    assert not is_match(g, i)


def test_empty_name_bins_match_via_tight_path_not_name():
    # Two unnamed bins at the same spot: must merge via the tight bin path (<=25m),
    # NOT via a bogus name_sim=1.0 on empty strings.
    j = rec(*BASE, "", org_type="drop_bin")
    k = rec(*BASE, "", org_type="drop_bin")
    assert j["brand_key"] is None and name_sim("", "") == 0.0
    assert is_match(j, k)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"PASS {t.__name__}")
    print(f"\nAll {len(tests)} dedup-logic tests passed.")
