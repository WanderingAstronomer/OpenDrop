"""Planet Aid grid-sweep scraper: parse upstream sites, dedupe on id, drop out-of-region bins.

Planet Aid's binlocator API returns the 20 nearest sites for a (lat,lon) point regardless of how
far away they are, so the scraper sweeps a grid over the region bbox and must (a) dedupe sites that
show up under several grid points, (b) drop sites whose geoPoint falls outside the region bbox
(margin 0.05), and (c) bump `fetch_failures` when a grid call is swallowed so the loader refuses to
closure-detect on an incomplete `seen`. These pin that contract plus the siteAddress regex parse and
the siteTypeId -> org_type mapping. No DB, no network — the HTTP client is faked.

Run: PYTHONPATH=. pytest tests/test_planet_aid.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.regions import Region  # noqa: E402
from pipeline.scrapers import planet_aid  # noqa: E402

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
    HAVE_HYPOTHESIS = True
except Exception:  # noqa: BLE001
    HAVE_HYPOTHESIS = False


# --- fakes ---------------------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise_exc = raise_exc

    def raise_for_status(self):
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self):
        return self._payload


class _FakeClient:
    """Context-manager HTTP client returning one canned payload for every grid get().

    The scraper calls client.get(API, params={"latitude": lat, "longitude": lon}) once per grid
    cell; every cell sees the same payload, which is exactly how a real nearest-N API behaves for a
    cluster of bins and is what makes cross-cell dedup observable.
    """
    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise_exc = raise_exc
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        self.calls += 1
        return _FakeResp(self._payload, self._raise_exc)


class _RaisingGetClient:
    """Every get() raises -> exercises the swallowed-failure / fetch_failures path."""
    def __init__(self, exc):
        self._exc = exc
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        self.calls += 1
        raise self._exc


# --- fixtures ------------------------------------------------------------------------------------

def _site(sid, lat, lon, name="PA bin", address="500 Oak Ave Columbus,OH 43004", type_id="1"):
    """A canned upstream site dict using the exact JSON keys planet_aid reads."""
    return {
        "id": sid,
        "siteName": name,
        "geoPoint": {"latitude": lat, "longitude": lon},
        "siteAddress": address,
        "siteTypeId": type_id,
    }


def _compact_region():
    """A tight region whose bbox spans 0.10 deg each way -> step clamps to 0.13 -> a single grid
    cell, so each distinct payload is fetched exactly once (clean baseline for content assertions).
    center (40.0, -83.0). The contains() margin used by the scraper is 0.05 deg."""
    return Region("pa_compact", (39.95, -83.05, 40.05, -82.95), (40.0, -83.0), [], 25)


def _multi_cell_region():
    """A wider region (~0.40 deg span) that produces several grid cells, so the same payload is
    fetched from multiple points -> repeated ids must collapse to one record each (cross-cell dedup)."""
    return Region("pa_multi", (39.80, -83.20, 40.20, -82.80), (40.0, -83.0), [], 25)


# --- normal multi-record parse -------------------------------------------------------------------

def test_fetch_parses_multiple_records(monkeypatch):
    a = _site("a1", 40.00, -83.00)
    b = _site("b2", 40.01, -83.01, name="Bin B")
    payload = [a, b]
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: _FakeClient(payload))

    recs = list(planet_aid.PlanetAidScraper().fetch(_compact_region()))

    by_ref = {r.source_ref: r for r in recs}
    assert set(by_ref) == {"a1", "b2"}

    r = by_ref["a1"]
    assert r.lat == 40.00 and r.lon == -83.00
    assert r.org_name == "Planet Aid"
    assert r.org_type == "drop_bin"               # siteTypeId "1" -> not in {20,21}
    assert r.accepted_items == ["clothing", "shoes"]
    assert r.hours == {"always": True}            # drop_bin always-open hours
    # siteAddress "500 Oak Ave Columbus,OH 43004": the comma splits the locality from STATE ZIP,
    # then the locality's trailing token is the city and the rest is the (full) street.
    assert r.address_line == "500 Oak Ave"
    assert r.city == "Columbus"
    assert r.state == "OH"
    assert r.postal_code == "43004"
    assert by_ref["b2"].name == "Bin B"


def test_site_name_fallback_when_missing(monkeypatch):
    s = _site("c3", 40.0, -83.0)
    s.pop("siteName")
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: _FakeClient([s]))

    rec = next(iter(planet_aid.PlanetAidScraper().fetch(_compact_region())))
    assert rec.name == "Planet Aid donation bin"   # default name when siteName absent


def test_site_type_id_maps_to_org_type(monkeypatch):
    bin_site = _site("d-bin", 40.00, -83.00, type_id="1")
    center20 = _site("d-20", 40.01, -83.00, type_id="20")
    center21 = _site("d-21", 40.00, -83.01, type_id="21")
    monkeypatch.setattr(planet_aid, "PoliteClient",
                        lambda *ar, **kw: _FakeClient([bin_site, center20, center21]))

    by_ref = {r.source_ref: r for r in planet_aid.PlanetAidScraper().fetch(_compact_region())}
    assert by_ref["d-bin"].org_type == "drop_bin"
    assert by_ref["d-20"].org_type == "donation_center"
    assert by_ref["d-21"].org_type == "donation_center"
    # donation_center carries no always-open hours; drop_bin does.
    assert by_ref["d-20"].hours is None
    assert by_ref["d-bin"].hours == {"always": True}


# --- address parsing edge cases ------------------------------------------------------------------

def test_unparseable_address_becomes_street_only(monkeypatch):
    # An address with no comma can't match _ADDR (which keys on the "<locality>,<STATE> <ZIP>"
    # shape) -> the whole stripped string is kept as address_line; city/state/postal stay None.
    s = _site("e-noparse", 40.0, -83.0, address="  Some Place With No Comma  ")
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: _FakeClient([s]))

    rec = next(iter(planet_aid.PlanetAidScraper().fetch(_compact_region())))
    assert rec.address_line == "Some Place With No Comma"
    assert rec.city is None and rec.state is None and rec.postal_code is None


def test_double_space_splits_full_street_from_city(monkeypatch):
    # The feed's real format separates street from city with a DOUBLE space; the parser must keep
    # the FULL street ("6501 Ducketts Ln") and the city ("Elkridge") rather than truncating the
    # street to just the house number (the pre-fix _ADDR regex bug).
    s = _site("e-dbl", 40.0, -83.0, address="6501 Ducketts Ln  Elkridge,MD 21075")
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: _FakeClient([s]))

    rec = next(iter(planet_aid.PlanetAidScraper().fetch(_compact_region())))
    assert rec.address_line == "6501 Ducketts Ln"
    assert rec.city == "Elkridge"
    assert rec.state == "MD" and rec.postal_code == "21075"


def test_single_token_locality_is_street_only(monkeypatch):
    # "<CITY>,<STATE> <ZIP>" with no street -> the lone locality token is kept as the street and
    # city stays None, while state/postal are still parsed off the comma-delimited tail.
    s = _site("e-1tok", 40.0, -83.0, address="Elkridge,MD 21075")
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: _FakeClient([s]))

    rec = next(iter(planet_aid.PlanetAidScraper().fetch(_compact_region())))
    assert rec.address_line == "Elkridge"
    assert rec.city is None
    assert rec.state == "MD" and rec.postal_code == "21075"


def test_empty_address_leaves_all_address_fields_none(monkeypatch):
    s = _site("e-empty", 40.0, -83.0, address="")
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: _FakeClient([s]))

    rec = next(iter(planet_aid.PlanetAidScraper().fetch(_compact_region())))
    assert rec.address_line is None
    assert rec.city is None and rec.state is None and rec.postal_code is None


# --- deduplication --------------------------------------------------------------------------------

def test_dedupe_repeated_ids_within_payload(monkeypatch):
    # Same id twice in one response -> a single record. Distinct coords on the dup must not matter;
    # the first occurrence wins and the second is skipped by the `seen` set.
    first = _site("dup", 40.00, -83.00)
    again = _site("dup", 40.02, -83.02)   # same id, different coords
    other = _site("uniq", 40.01, -83.01)
    monkeypatch.setattr(planet_aid, "PoliteClient",
                        lambda *ar, **kw: _FakeClient([first, again, other]))

    recs = list(planet_aid.PlanetAidScraper().fetch(_compact_region()))
    refs = [r.source_ref for r in recs]
    assert sorted(refs) == ["dup", "uniq"]        # "dup" appears exactly once
    assert refs.count("dup") == 1
    dup_rec = next(r for r in recs if r.source_ref == "dup")
    assert (dup_rec.lat, dup_rec.lon) == (40.00, -83.00)   # first occurrence kept


def test_dedupe_across_grid_cells(monkeypatch):
    # A multi-cell region fetches the same payload from several grid points; each id must still
    # yield exactly one record (cross-cell dedup on `seen`).
    client = _FakeClient([_site("g1", 40.00, -83.00), _site("g2", 40.01, -83.01)])
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: client)

    recs = list(planet_aid.PlanetAidScraper().fetch(_multi_cell_region()))
    refs = [r.source_ref for r in recs]
    assert client.calls > 1                        # the grid really did sweep multiple cells
    assert sorted(refs) == ["g1", "g2"]            # ...yet each id surfaces once
    assert len(refs) == len(set(refs))


# --- region.contains() filter --------------------------------------------------------------------

def test_out_of_region_sites_dropped(monkeypatch):
    inside = _site("in", 40.00, -83.00)            # squarely inside bbox
    border = _site("border", 40.08, -83.00)        # 0.03 deg past north edge (40.05) -> within margin
    far = _site("far", 41.50, -83.00)              # ~1.5 deg north -> a neighbouring region
    monkeypatch.setattr(planet_aid, "PoliteClient",
                        lambda *ar, **kw: _FakeClient([inside, border, far]))

    refs = {r.source_ref for r in planet_aid.PlanetAidScraper().fetch(_compact_region())}
    assert "in" in refs and "border" in refs       # inside + within-margin border kept
    assert "far" not in refs                        # nearest-N straggler dropped


def test_missing_geopoint_skipped(monkeypatch):
    good = _site("ok", 40.00, -83.00)
    no_geo = {"id": "no-geo", "siteName": "x", "siteAddress": "", "siteTypeId": "1"}  # no geoPoint
    null_lat = _site("null-lat", 40.0, -83.0)
    null_lat["geoPoint"]["latitude"] = None
    monkeypatch.setattr(planet_aid, "PoliteClient",
                        lambda *ar, **kw: _FakeClient([good, no_geo, null_lat]))

    refs = {r.source_ref for r in planet_aid.PlanetAidScraper().fetch(_compact_region())}
    assert refs == {"ok"}                            # both geo-less sites skipped


def test_missing_or_empty_id_skipped(monkeypatch):
    good = _site("real", 40.00, -83.00)
    no_id = {"siteName": "x", "geoPoint": {"latitude": 40.0, "longitude": -83.0},
             "siteAddress": "", "siteTypeId": "1"}            # id absent -> "" -> skipped
    empty_id = _site("", 40.0, -83.0)                          # id "" -> skipped
    monkeypatch.setattr(planet_aid, "PoliteClient",
                        lambda *ar, **kw: _FakeClient([good, no_id, empty_id]))

    refs = {r.source_ref for r in planet_aid.PlanetAidScraper().fetch(_compact_region())}
    assert refs == {"real"}


# --- failure handling ----------------------------------------------------------------------------

def test_fetch_failures_incremented_on_swallowed_call(monkeypatch):
    # Every grid get() raises; the scraper swallows each and bumps fetch_failures, yielding nothing.
    monkeypatch.setattr(planet_aid, "PoliteClient",
                        lambda *ar, **kw: _RaisingGetClient(RuntimeError("boom")))

    scraper = planet_aid.PlanetAidScraper()
    recs = list(scraper.fetch(_compact_region()))   # single-cell region -> exactly one failure
    assert recs == []
    assert scraper.fetch_failures == 1


def test_fetch_failures_counts_every_failed_cell(monkeypatch):
    monkeypatch.setattr(planet_aid, "PoliteClient",
                        lambda *ar, **kw: _RaisingGetClient(ValueError("nope")))

    scraper = planet_aid.PlanetAidScraper()
    list(scraper.fetch(_multi_cell_region()))       # several cells, all fail
    assert scraper.fetch_failures > 1


def test_raise_for_status_failure_is_swallowed(monkeypatch):
    # A non-2xx upstream (raise_for_status raises) is treated as a swallowed grid failure too.
    monkeypatch.setattr(planet_aid, "PoliteClient",
                        lambda *ar, **kw: _FakeClient([_site("x", 40.0, -83.0)],
                                                      raise_exc=RuntimeError("503")))
    scraper = planet_aid.PlanetAidScraper()
    recs = list(scraper.fetch(_compact_region()))
    assert recs == []
    assert scraper.fetch_failures == 1


# --- empty / malformed payloads ------------------------------------------------------------------

def test_empty_list_payload_yields_nothing(monkeypatch):
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: _FakeClient([]))
    assert list(planet_aid.PlanetAidScraper().fetch(_compact_region())) == []


def test_none_payload_yields_nothing(monkeypatch):
    # API returned null/None instead of a list -> `for site in data or []` -> nothing.
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *ar, **kw: _FakeClient(None))
    assert list(planet_aid.PlanetAidScraper().fetch(_compact_region())) == []


# --- property-based: id-uniqueness + in-region invariants ----------------------------------------

if HAVE_HYPOTHESIS:

    _ids = st.text(alphabet="abcdefghijklmnop0123456789", min_size=1, max_size=6)
    # Coords spanning well beyond the compact region's bbox+margin so both kept and dropped sites occur.
    _lats = st.floats(min_value=39.0, max_value=41.0, allow_nan=False, allow_infinity=False)
    _lons = st.floats(min_value=-84.0, max_value=-82.0, allow_nan=False, allow_infinity=False)
    _site_strat = st.fixed_dictionaries({
        "id": _ids,
        "lat": _lats,
        "lon": _lons,
        "type_id": st.sampled_from(["1", "5", "20", "21", "99"]),
    })

    @settings(max_examples=200, deadline=None)
    @given(st.lists(_site_strat, max_size=12))
    def test_property_dedup_and_in_region(raw):
        # Note: Hypothesis-driven tests can't take the pytest `monkeypatch` fixture (fixtures don't
        # compose with @given args), so we patch/restore PoliteClient by hand in a try/finally.
        region = _compact_region()
        payload = [_site(d["id"], d["lat"], d["lon"], type_id=d["type_id"]) for d in raw]
        original = planet_aid.PoliteClient
        planet_aid.PoliteClient = lambda *ar, **kw: _FakeClient(payload)
        try:
            recs = list(planet_aid.PlanetAidScraper().fetch(region))
        finally:
            planet_aid.PoliteClient = original
        refs = [r.source_ref for r in recs]

        # Invariant 1: every yielded ref is unique (the `seen` set guarantees no duplicates).
        assert len(refs) == len(set(refs))
        # Invariant 2: every yielded record's coords lie within the bbox + 0.05 margin.
        for r in recs:
            assert region.contains(r.lat, r.lon, margin=0.05)
        # Invariant 3: every yielded ref came from the input (no fabricated records).
        input_ids = {d["id"] for d in raw}
        assert set(refs) <= input_ids
        # Invariant 4: a clean run (no swallowed failures) leaves fetch_failures at 0.
        assert planet_aid.PlanetAidScraper().fetch_failures == 0


if __name__ == "__main__":
    print("Run with: PYTHONPATH=. pytest tests/test_planet_aid.py")
