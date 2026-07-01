"""Property-based + edge-case tests for the dedup MATCH PREDICATE (pipeline.dedup.is_match).

No DB, no network: is_match / the common.py helpers are pure functions, called directly.
The DB-bound helpers (find_match, choose_canonical, merge, run) need a live connection and are
covered elsewhere; this file pins the pure predicate's invariants.

Invariants established directly from pipeline/dedup.py::is_match and pipeline/common.py:
  - is_match is SYMMETRIC: is_match(a, b) == is_match(b, a).
    (haversine_m, name_sim, brand_equal are all symmetric; the tier-2 house_number test reduces to
     "both house numbers equal and non-None", which is symmetric.)
  - REFLEXIVITY (conditional): a branded record with a non-empty name always matches itself (dist 0,
    name_sim>=0.99); an unbranded bin (drop_bin/donation_center) always matches itself (dist 0 <= 25m).
  - DISTANCE GATE: dist > 600 m => never a match, regardless of name/brand/house_number.
  - A record with lat is None never matches anything.

Run: PYTHONPATH=. pytest tests/test_dedup_property.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math  # noqa: E402

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from pipeline.common import brand_key, haversine_m, name_sim  # noqa: E402
from pipeline.dedup import is_match  # noqa: E402

BASE = (39.9600, -82.9900)


def rec(lat, lon, name, org_type="charity_store", house_number=None, brand=None):
    return {
        "lat": lat,
        "lon": lon,
        "name": name,
        "brand_key": brand if brand is not None else brand_key(name),
        "org_type": org_type,
        "house_number": house_number,
    }


# ----- meters<->degrees helper, so we can place points at a known great-circle distance -----
def _offset_lat(base_lat, base_lon, meters):
    """Return a point `meters` due north of (base_lat, base_lon). 1 deg lat ~= 111195 m here."""
    # r used by haversine_m is 6371008.8 m; 1 rad of latitude = r meters.
    dlat_deg = math.degrees(meters / 6371008.8)
    return (base_lat + dlat_deg, base_lon)


def test_offset_helper_matches_haversine():
    # Sanity: the helper actually produces the requested distance under the module's haversine.
    for m in (10.0, 100.0, 250.0, 500.0, 700.0):
        la, lo = _offset_lat(*BASE, m)
        d = haversine_m(BASE[0], BASE[1], la, lo)
        assert abs(d - m) < 0.5, (m, d)


# ===================== EDGE / COMMON CASES =====================

def test_none_lat_never_matches_either_order():
    a = rec(None, None, "Goodwill")
    b = rec(*BASE, "Goodwill")
    assert is_match(a, b) is False
    assert is_match(b, a) is False
    # both None
    c = rec(None, None, "Goodwill")
    assert is_match(a, c) is False


def test_exactly_300m_same_brand_matches():
    a = rec(*BASE, "Goodwill Columbus")
    la, lo = _offset_lat(*BASE, 299.0)  # strictly inside the <=300 primary band
    b = rec(la, lo, "Goodwill Columbus")
    assert haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) <= 300
    assert is_match(a, b)


def test_just_past_300m_same_brand_no_housenum_no_match():
    # Same brand, good name_sim, but 301..600 m and NO house number => primary fails, tier-2 can't fire.
    a = rec(*BASE, "Goodwill Columbus")
    la, lo = _offset_lat(*BASE, 350.0)
    b = rec(la, lo, "Goodwill Columbus")
    d = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    assert 300 < d <= 600
    assert not is_match(a, b)


def test_tier2_recovers_350m_with_house_number():
    a = rec(*BASE, "Goodwill", house_number="500")
    la, lo = _offset_lat(*BASE, 350.0)
    b = rec(la, lo, "Goodwill", house_number="500")
    assert is_match(a, b)
    assert is_match(b, a)


def test_tier2_blocked_by_mismatched_house_number():
    a = rec(*BASE, "Goodwill", house_number="500")
    la, lo = _offset_lat(*BASE, 350.0)
    b = rec(la, lo, "Goodwill", house_number="999")
    assert not is_match(a, b)


def test_beyond_600m_same_brand_same_housenum_no_match():
    a = rec(*BASE, "Goodwill", house_number="500")
    la, lo = _offset_lat(*BASE, 650.0)  # past the hard 600 m gate
    b = rec(la, lo, "Goodwill", house_number="500")
    assert haversine_m(a["lat"], a["lon"], b["lat"], b["lon"]) > 600
    assert not is_match(a, b)


def test_low_name_sim_same_brand_close_no_match():
    # Same brand token (svdp) but the long needle vs nothing-else-in-common keeps name_sim < 0.4.
    a = rec(*BASE, "Society of St Vincent de Paul")
    la, lo = _offset_lat(*BASE, 50.0)
    b = rec(la, lo, "SVDP")  # brand_key None for "svdp"? check: normalized 'svdp' has no needle -> None
    # Force same brand to isolate the name_sim gate:
    b["brand_key"] = "svdp"
    assert brand_key("Society of St Vincent de Paul") == "svdp"
    assert name_sim(a["name"], b["name"]) < 0.4
    assert not is_match(a, b)


def test_unbranded_bins_25m_boundary():
    a = rec(*BASE, "Donation bin", org_type="drop_bin")
    near = _offset_lat(*BASE, 20.0)
    b = rec(*near, "Donation bin", org_type="drop_bin")
    assert is_match(a, b)
    far = _offset_lat(*BASE, 30.0)
    c = rec(*far, "Donation bin", org_type="drop_bin")
    assert not is_match(a, c)


def test_unbranded_bins_different_org_type_no_match():
    a = rec(*BASE, "bin", org_type="drop_bin")
    b = rec(*_offset_lat(*BASE, 5.0), "bin", org_type="donation_center")
    assert not is_match(a, b)


def test_unbranded_non_bin_type_no_match_even_colocated():
    # org_type not in {drop_bin, donation_center} => the unbranded co-located path never fires.
    a = rec(*BASE, "", org_type="charity_store")
    b = rec(*BASE, "", org_type="charity_store")
    assert a["brand_key"] is None and b["brand_key"] is None
    assert not is_match(a, b)


def test_one_branded_one_unbranded_no_match():
    # brand_equal is False (None != 'goodwill') AND not (both None) => falls through to no match.
    a = rec(*BASE, "Goodwill")
    b = rec(*_offset_lat(*BASE, 10.0), "Random Thrift", org_type="drop_bin")
    assert a["brand_key"] == "goodwill" and b["brand_key"] is None
    assert not is_match(a, b)


# ===================== PROPERTY-BASED (Hypothesis) =====================

# Reusable strategies
_lat = st.floats(min_value=39.0, max_value=41.0, allow_nan=False, allow_infinity=False)
_lon = st.floats(min_value=-84.0, max_value=-82.0, allow_nan=False, allow_infinity=False)
_name = st.sampled_from(
    ["Goodwill", "Goodwill Columbus", "Salvation Army", "Donation bin",
     "Clothing bin", "Random Thrift", "", "The Salvation Army Family Store"]
)
_org = st.sampled_from(["charity_store", "drop_bin", "donation_center"])
_hn = st.sampled_from([None, "100", "500", "999"])


@st.composite
def _records(draw):
    name = draw(_name)
    return rec(
        draw(_lat), draw(_lon), name,
        org_type=draw(_org),
        house_number=draw(_hn),
    )


@given(a=_records(), b=_records())
@settings(max_examples=400)
def test_property_symmetry(a, b):
    """is_match(a, b) must equal is_match(b, a) for ANY pair of records."""
    assert is_match(a, b) == is_match(b, a)


@given(
    lat=_lat, lon=_lon,
    name=st.sampled_from(["Goodwill", "Salvation Army", "Savers", "Goodwill Columbus"]),
    hn=_hn,
)
@settings(max_examples=200)
def test_property_branded_record_matches_itself(lat, lon, name, hn):
    """A branded record with a non-empty name always matches itself (dist 0, name_sim ~1)."""
    r = rec(lat, lon, name, house_number=hn)
    assert r["brand_key"] is not None  # all sampled names are branded
    assert is_match(r, r)


@given(
    lat=_lat, lon=_lon,
    org=st.sampled_from(["drop_bin", "donation_center"]),
    name=st.sampled_from(["", "Donation bin", "Clothing bin"]),
)
@settings(max_examples=200)
def test_property_unbranded_bin_matches_itself(lat, lon, org, name):
    """An unbranded bin (drop_bin/donation_center) always matches itself via the <=25 m path."""
    r = rec(lat, lon, name, org_type=org)
    assert r["brand_key"] is None
    assert is_match(r, r)


@given(
    a=_records(),
    bearing_name=_name,
    extra_m=st.floats(min_value=1.0, max_value=5000.0, allow_nan=False),
    org=_org, hn=_hn,
)
@settings(max_examples=400)
def test_property_beyond_600m_never_matches(a, bearing_name, extra_m, org, hn):
    """Any second point strictly more than 600 m away can NEVER match, whatever name/brand/housenum."""
    la, lo = _offset_lat(a["lat"], a["lon"], 600.0 + extra_m)
    b = rec(la, lo, bearing_name, org_type=org, house_number=hn)
    # Confirm the placement really is > 600 m under the module's own haversine.
    assert haversine_m(a["lat"], a["lon"], la, lo) > 600
    assert is_match(a, b) is False
    assert is_match(b, a) is False


@given(a=_records(), b=_records())
@settings(max_examples=300)
def test_property_none_lat_never_matches(a, b):
    """If either record has lat None, is_match is always False (both orders)."""
    a2 = dict(a, lat=None, lon=None)
    assert is_match(a2, b) is False
    assert is_match(b, a2) is False


if __name__ == "__main__":
    print("Run with: PYTHONPATH=. pytest tests/test_dedup_property.py")
