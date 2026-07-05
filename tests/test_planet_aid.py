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

from pipeline.common import haversine_m  # noqa: E402
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


# =================================================================================================
# Adaptive quadtree subdivision — a faithful nearest-N fake + the tests that prove completeness,
# termination, the fetch_failures contract, and backward-compat. The OLD _FakeClient tests above are
# left UNEDITED and passing: every one of them feeds < N_CAP sites, so the len(data) < N_CAP branch
# fires and NO cell ever subdivides — that is itself the machine-checked backward-compat proof.
# =================================================================================================

class _NearestNClient:
    """Context-manager HTTP client that faithfully simulates a nearest-N locator API over a FIXED
    universe of sites: each get() returns the `cap` sites nearest the queried (lat,lon), ranked by
    the SAME haversine_m the scraper uses (ties broken by id for determinism). This is what makes
    subdivision observable — a coarse cell over a dense cluster receives only the nearest `cap`, and
    only re-querying sub-cells recovers the rest. Records .calls and .centers; raise_on(lat,lon)->
    exc|None injects a per-cell failure. Pinning distance to haversine_m guarantees a test can never
    assert a completeness the scraper's own geometry cannot deliver."""

    def __init__(self, universe, cap=None, raise_on=None):
        self._universe = list(universe)
        self._cap = planet_aid.N_CAP if cap is None else cap
        self._raise_on = raise_on
        self.calls = 0
        self.centers = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        lat, lon = params["latitude"], params["longitude"]
        self.calls += 1
        self.centers.append((round(lat, 6), round(lon, 6)))
        if self._raise_on is not None:
            exc = self._raise_on(lat, lon)
            if exc is not None:
                raise exc

        def _key(site):
            gp = site.get("geoPoint") or {}
            return (haversine_m(lat, lon, gp["latitude"], gp["longitude"]), str(site.get("id") or ""))

        return _FakeResp(sorted(self._universe, key=_key)[: self._cap])


def _run_fetch(region, client, scraper=None):
    """Run PlanetAidScraper().fetch(region) with `client` patched in. Hypothesis-driven tests can't
    take the monkeypatch fixture, so patch/restore PoliteClient by hand. Returns (records, scraper)."""
    scraper = scraper or planet_aid.PlanetAidScraper()
    original = planet_aid.PoliteClient
    planet_aid.PoliteClient = lambda *a, **k: client
    try:
        return list(scraper.fetch(region)), scraper
    finally:
        planet_aid.PoliteClient = original


def _seed_count(region):
    """The number of top-level cells the sweep seeds for a region — the baseline (no-subdivision)
    call count, identical to the old flat grid's cell count."""
    return len(list(planet_aid._seed_cells(region.bbox)))


def _lattice(rows, cols, clat, clon, dlat, dlon, prefix="g"):
    """A regular rows x cols lattice of sites centered on (clat, clon)."""
    return [
        _site(f"{prefix}{i}-{j}", round(clat + (i - (rows - 1) / 2) * dlat, 6),
              round(clon + (j - (cols - 1) / 2) * dlon, 6))
        for i in range(rows) for j in range(cols)
    ]


def _colocated(n, clat=40.0, clon=-83.0, prefix="c"):
    """n sites within ~a few meters of one point (id-jittered by 1 microdegree ~ 0.1 m each) — the
    'mall' degenerate where d_max stays ~0 at every scale."""
    return [_site(f"{prefix}{k}", round(clat + k * 1e-6, 6), round(clon, 6)) for k in range(n)]


_BOUND = _seed_count(_compact_region()) * (4 ** (planet_aid.MAX_DEPTH + 1) - 1) // 3  # max cells, depth<=MAX_DEPTH


# --- completeness in dense areas (the core regression) -------------------------------------------

def test_dense_cluster_recovered():
    # 64 bins packed inside the single seed cell of _compact_region: one center query returns only
    # the nearest 20, so the OLD flat grid yielded 20. The quadtree must subdivide and recover ALL 64.
    universe = _lattice(8, 8, 40.0, -83.0, 0.008, 0.008)
    client = _NearestNClient(universe)
    recs, _ = _run_fetch(_compact_region(), client)
    assert {r.source_ref for r in recs} == {s["id"] for s in universe}   # all 64 recovered
    assert client.calls > 1                                              # it really did subdivide


def test_sparse_no_subdivision():
    # 5 bins spread across a 16-cell region: every cell returns < N_CAP, so no cell splits and the
    # call count is exactly the baseline seed-cell count — sparse coverage stays as cheap as today.
    region = _multi_cell_region()
    universe = [_site("s1", 39.90, -83.10), _site("s2", 40.00, -83.00), _site("s3", 40.10, -82.90),
                _site("s4", 39.95, -82.95), _site("s5", 40.05, -83.05)]
    client = _NearestNClient(universe)
    recs, _ = _run_fetch(region, client)
    assert {r.source_ref for r in recs} == {s["id"] for s in universe}
    assert client.calls == _seed_count(region)


def test_at_cap_but_covered_no_subdivide():
    # A full-cap (20-site) cell whose sites are spread WIDER than the cell, so d_max already reaches
    # past the corner (d_max >= SAFETY*corner_m): the covering disk contains the whole cell, so the
    # AND-criterion must NOT over-subdivide despite len(data) == N_CAP.
    universe = _lattice(4, 5, 40.0, -83.0, 0.16 / 3, 0.16 / 4)   # 20 sites spanning ~0.16 deg
    client = _NearestNClient(universe)
    recs, _ = _run_fetch(_compact_region(), client)
    assert {r.source_ref for r in recs} == {s["id"] for s in universe}
    assert client.calls == _seed_count(_compact_region())       # exactly baseline: no extra queries


def test_out_of_region_used_for_dmax_but_dropped():
    # 19 bins clustered at the region center + 1 far bin OUTSIDE the region. All 20 are returned (the
    # cap), so the far bin sets a large d_max that marks the cell complete (no subdivision), yet the
    # far bin is dropped from output. If out-of-region sites were NOT counted for d_max, the tight
    # cluster's small d_max would force a spurious subdivision -> client.calls > 1.
    cluster = [_site(f"n{k}", round(40.0 + (k % 5) * 1e-4, 6), round(-83.0 + (k // 5) * 1e-4, 6))
               for k in range(19)]
    far = _site("far", 40.30, -83.0)                            # ~31 km north, well past the +0.05 margin
    client = _NearestNClient(cluster + [far])
    recs, _ = _run_fetch(_compact_region(), client)
    refs = {r.source_ref for r in recs}
    assert "far" not in refs                                    # out-of-region -> dropped from output
    assert refs == {s["id"] for s in cluster}                  # the 19 in-region bins yielded
    assert client.calls == _seed_count(_compact_region())      # far bin's distance marked the cell complete


def test_cushion_pins_corner_recovery():
    # The SAFETY cushion must be a REAL margin above bare corner coverage, not ~1.0. Build a full-cap
    # seed cell whose 20 in-region bins put d_max ~5% INSIDE the band [corner_m, SAFETY*corner_m),
    # plus one extra in-region bin sitting diagonally BEYOND that d_max. Only the cushion forces the
    # subdivision that recovers the diagonal bin; at SAFETY ~ 1.0 (bare corner) the cell is declared
    # complete and the bin is silently dropped. This test therefore FAILS if the 1.10 cushion is
    # weakened toward 1.0 — pinning the deliberate margin that recovers real corner/diagonal bins.
    region = _compact_region()
    clat, clon, h0, _ = list(planet_aid._seed_cells(region.bbox))[0]   # exactly one seed cell
    corner = planet_aid._corner_m(clat, clon, h0)
    r1 = corner * 1.05                                                 # ring ~5% into the cushion band
    ring = [_site(f"r{k}", round(clat - r1 / 111320.0 + (k - 10) * 2e-4, 6),
                  round(clon + (k - 10) * 2e-4, 6)) for k in range(planet_aid.N_CAP)]
    hidden = _site("hidden", 40.08, -82.90)                            # in-region, diagonal, beyond d_max
    assert region.contains(40.08, -82.90, margin=0.05)
    client = _NearestNClient(ring + [hidden])
    recs, _ = _run_fetch(region, client)
    ids = {r.source_ref for r in recs}
    assert ids == {s["id"] for s in ring} | {"hidden"}                # all 21 in-region bins recovered...
    assert client.calls > 1                                           # ...because the cushion forced a split


def test_dense_cluster_across_seed_cell_seam():
    # Completeness ACROSS a seed-cell boundary (the single-seed oracle can't reach this): a dense
    # cluster straddling the seam between two adjacent seed cells must be recovered in full.
    region = _multi_cell_region()
    cells = list(planet_aid._seed_cells(region.bbox))
    assert len(cells) > 1
    (a_lat, a_lon, _, _), (_, b_lon, _, _) = cells[0], cells[1]
    universe = _lattice(8, 8, a_lat, (a_lon + b_lon) / 2.0, 0.006, 0.006)   # 64 bins across the seam
    oracle = {s["id"] for s in universe
              if region.contains(s["geoPoint"]["latitude"], s["geoPoint"]["longitude"], margin=0.05)}
    client = _NearestNClient(universe)
    recs, _ = _run_fetch(region, client)
    assert {r.source_ref for r in recs} == oracle                     # every in-region seam bin recovered
    assert len(oracle) >= planet_aid.N_CAP                            # the cluster really exceeded one cap


# --- termination + the co-located degenerate -----------------------------------------------------

def test_colocated_cluster_terminates():
    # 40 bins within ~4 m of one point => d_max ~ 0 at every scale => saturated forever. The size/
    # depth floor must stop the recursion; the API's nearest-20 there are the accepted residual.
    client = _NearestNClient(_colocated(40))
    recs, _ = _run_fetch(_compact_region(), client)
    assert client.calls > 1                                     # it subdivided (toward the floor)...
    assert client.calls <= _BOUND                               # ...but a bounded number of times
    assert len({r.source_ref for r in recs}) >= planet_aid.N_CAP  # at least the nearest-20 kept


def test_query_budget_valve(monkeypatch):
    # A tiny per-region budget over a saturating universe: the loop stops subdividing at the budget,
    # drains no further, and still returns (graceful degrade).
    monkeypatch.setenv("PLANET_AID_MAX_QUERIES", "5")
    client = _NearestNClient(_colocated(40))
    recs, _ = _run_fetch(_compact_region(), client)
    assert client.calls <= 5
    assert len(recs) > 0                                        # returned records, did not hang


def test_max_queries_env_override_and_fallback(monkeypatch):
    # The per-region budget is env-tunable; a malformed value falls back to the default (never raises).
    monkeypatch.setenv("PLANET_AID_MAX_QUERIES", "42")
    assert planet_aid._max_queries() == 42
    monkeypatch.setenv("PLANET_AID_MAX_QUERIES", "not-an-int")
    assert planet_aid._max_queries() == planet_aid._DEFAULT_MAX_QUERIES
    monkeypatch.delenv("PLANET_AID_MAX_QUERIES", raising=False)
    assert planet_aid._max_queries() == planet_aid._DEFAULT_MAX_QUERIES


# --- fetch_failures contract under subdivision ---------------------------------------------------

def test_failure_mid_subdivision_bumps_once_and_is_not_subdivided():
    # A dense cluster forces the seed cell to split into 4 children; exactly ONE child's query fails.
    # The failure bumps fetch_failures by exactly 1 (never 4x), the failed cell is NOT subdivided,
    # and the other children still yield their bins.
    failed = (39.9825, -83.0175)                               # one of the seed cell's 4 quadrant centers

    def _raise_at(lat, lon):
        if abs(lat - failed[0]) < 1e-6 and abs(lon - failed[1]) < 1e-6:
            return RuntimeError("cell down")
        return None

    client = _NearestNClient(_lattice(5, 5, 40.0, -83.0, 0.004, 0.004), raise_on=_raise_at)
    recs, scraper = _run_fetch(_compact_region(), client)
    assert scraper.fetch_failures == 1                          # exactly one swallowed failure
    assert client.centers.count(failed) == 1                   # the failed cell was queried once...
    grandchildren = {(round(failed[0] + a, 6), round(failed[1] + b, 6))
                     for a in (-0.01625, 0.01625) for b in (-0.01625, 0.01625)}
    assert grandchildren.isdisjoint(client.centers)            # ...and never subdivided (no grandchildren)
    assert len(recs) > 0                                        # sibling cells still produced records


def test_floor_does_not_bump_fetch_failures():
    # Hitting the size/depth floor on a dense cluster is a fetch SUCCESS (the API answered), so it
    # must NOT bump fetch_failures — a clean dense run still ends at 0 (property Invariant 4).
    client = _NearestNClient(_colocated(40))
    _, scraper = _run_fetch(_compact_region(), client)
    assert scraper.fetch_failures == 0


def test_non_numeric_coord_skipped(monkeypatch):
    # A present-but-non-numeric geoPoint coord (dirty upstream row) must be SKIPPED like a missing
    # geoPoint — not crash out of fetch() and abort the whole region's load. It is a data-quality
    # skip, so it does NOT bump fetch_failures (a fetch failure is a swallowed request, not a bad row).
    good = _site("ok", 40.0, -83.0)
    bad_lat = _site("bad-lat", 40.0, -83.0)
    bad_lat["geoPoint"]["latitude"] = "N/A"          # non-numeric string -> float() would raise
    bad_lon = _site("bad-lon", 40.0, -83.0)
    bad_lon["geoPoint"]["longitude"] = {}            # wrong type -> float() would raise
    monkeypatch.setattr(planet_aid, "PoliteClient", lambda *a, **k: _FakeClient([good, bad_lat, bad_lon]))
    scraper = planet_aid.PlanetAidScraper()
    recs = list(scraper.fetch(_compact_region()))
    assert {r.source_ref for r in recs} == {"ok"}    # dirty rows skipped, good row kept
    assert scraper.fetch_failures == 0               # a dirty coord is not a fetch failure


# --- property-based: completeness oracle, termination, invariants, backward-compat ----------------

def test_hypothesis_is_available_for_property_suite():
    # The property-based completeness/termination proofs live behind `if HAVE_HYPOTHESIS`; if the dep
    # is missing they silently VANISH and the suite still reports green. Fail loudly instead so a
    # mis-provisioned runner is caught. hypothesis is pinned in backend/requirements-dev.txt.
    assert HAVE_HYPOTHESIS, "hypothesis not installed — the property-based proofs did not run"

if HAVE_HYPOTHESIS:

    # Distinct points on a 0.01-deg (~1.1 km) lattice, comfortably coarser than the ~556 m floor, so
    # no floor cell ever holds > N_CAP bins -> the completeness guarantee is exact (no residual).
    _cells = st.lists(st.tuples(st.integers(-5, 5), st.integers(-5, 5)), max_size=60, unique=True)
    # Arbitrary (possibly co-located) coords across the compact region + its margin, for termination
    # and invariant properties where subdivision is genuinely exercised.
    _pts = st.lists(
        st.tuples(st.floats(39.90, 40.10, allow_nan=False, allow_infinity=False),
                  st.floats(-83.10, -82.90, allow_nan=False, allow_infinity=False)),
        max_size=40,
    )

    @settings(max_examples=100, deadline=None)
    @given(_cells)
    def test_property_completeness_oracle(cells):
        # THE rigorous proof: the set of in-region bins the sweep yields EXACTLY equals the brute-force
        # oracle of all bins inside the swept bbox — subdivision recovers precisely the bins the coarse
        # grid dropped, no more (no fabrication), no fewer (no misses).
        region = _compact_region()
        universe = [_site(f"o{idx}", round(40.0 + i * 0.01, 6), round(-83.0 + j * 0.01, 6))
                    for idx, (i, j) in enumerate(cells)]
        client = _NearestNClient(universe)
        recs, _ = _run_fetch(region, client)
        yielded = {r.source_ref for r in recs}
        oracle = {s["id"] for s in universe
                  if region.contains(s["geoPoint"]["latitude"], s["geoPoint"]["longitude"], margin=0.05)}
        assert yielded == oracle

    @settings(max_examples=150, deadline=None)
    @given(_pts)
    def test_property_terminates_and_bounded(pts):
        # ANY universe, including adversarial co-location, must return and stay within the analytic
        # quadtree cell bound (proves no unbounded recursion / runaway call count).
        universe = [_site(f"t{k}", round(la, 6), round(lo, 6)) for k, (la, lo) in enumerate(pts)]
        client = _NearestNClient(universe)
        recs, _ = _run_fetch(_compact_region(), client)
        assert client.calls <= _BOUND
        assert isinstance(recs, list)

    @settings(max_examples=150, deadline=None)
    @given(_pts)
    def test_property_invariants_under_subdivision(pts):
        # Re-assert the core invariants with the subdivision path exercised: unique refs, every coord
        # in-region, no fabricated ids, and a clean run leaves fetch_failures at 0.
        region = _compact_region()
        universe = [_site(f"i{k}", round(la, 6), round(lo, 6)) for k, (la, lo) in enumerate(pts)]
        client = _NearestNClient(universe)
        recs, scraper = _run_fetch(region, client)
        refs = [r.source_ref for r in recs]
        assert len(refs) == len(set(refs))                     # unique
        for r in recs:
            assert region.contains(r.lat, r.lon, margin=0.05)  # in-region
        assert set(refs) <= {s["id"] for s in universe}        # no fabrication
        assert scraper.fetch_failures == 0                     # clean run

    @settings(max_examples=100, deadline=None)
    @given(st.lists(
        st.tuples(st.floats(39.85, 40.15, allow_nan=False, allow_infinity=False),
                  st.floats(-83.25, -82.75, allow_nan=False, allow_infinity=False)),
        max_size=planet_aid.N_CAP - 1))
    def test_property_small_payload_never_subdivides(pts):
        # Machine-checked backward-compat: any universe with < N_CAP bins yields exactly the baseline
        # seed-cell call count (the len(data) < N_CAP short-circuit blocks every subdivision).
        region = _multi_cell_region()
        universe = [_site(f"s{k}", round(la, 6), round(lo, 6)) for k, (la, lo) in enumerate(pts)]
        client = _NearestNClient(universe)
        _run_fetch(region, client)
        assert client.calls == _seed_count(region)


if __name__ == "__main__":
    print("Run with: PYTHONPATH=. pytest tests/test_planet_aid.py")
