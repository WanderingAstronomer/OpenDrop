"""Tests for the Wearable Collections NYC greenmarket scraper (no DB, no network).

This scraper is NYC-only. fetch() first short-circuits regions that don't overlap NYC_BBOX
(so a national run never geocodes 8 Manhattan/Brooklyn/Queens sites against, say, Montana),
then geocodes each known SITE via Nominatim and yields a NormalizedRecord only when the geocoded
point lands inside the region (with a 0.1 deg margin). A site it can't place bumps fetch_failures
so the loader knows `seen` is incomplete and won't closure-retire live bins.

These pin: the _overlaps bbox predicate (incl. its symmetry invariant), the non-NYC skip (no
geocoding at all), the happy path (every in-region SITE -> a drop_bin record with the textile
accepted_items), the unplaceable-site path (fetch_failures bumped, site skipped), and the
in-NYC-but-out-of-region filter. The module's PoliteClient is faked; no network is touched.
Run: PYTHONPATH=. pytest tests/test_wearable_collections.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from pipeline.regions import Region  # noqa: E402
from pipeline.scrapers import wearable_collections as wc  # noqa: E402


# --- fakes ---------------------------------------------------------------------------------------

class _FakeResp:
    """Nominatim returns a JSON array; _geocode reads r.json() and uses d[0]["lat"]/["lon"]."""
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Context-manager HTTP client. `responder(url, params)` -> payload list for each get().

    Also records every query string it was asked to geocode so a test can assert that NO
    geocoding happened (non-NYC short-circuit) or that all 8 SITES were geocoded.
    """
    def __init__(self, responder):
        self._responder = responder
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, params=None):
        params = params or {}
        self.queries.append(params.get("q"))
        return _FakeResp(self._responder(url, params))


def _patch_client(monkeypatch, client):
    """Pin the module's PoliteClient to return our fake for any constructor args."""
    monkeypatch.setattr(wc, "PoliteClient", lambda *a, **k: client)
    return client


# A point comfortably inside NYC_BBOX (Manhattan-ish): used as the canned geocode for every site.
NYC_LAT, NYC_LON = 40.73, -73.99


def _nyc_region(bbox=(40.40, -74.30, 41.00, -73.65)):
    """A region whose bbox IS NYC_BBOX by default (so it overlaps and contains NYC points)."""
    return Region("nyc_t", bbox, (40.70, -73.95), [], 25)


def _all_to(lat, lon):
    """Responder that geocodes every query to the same (lat, lon) (as Nominatim string fields)."""
    return lambda url, params: [{"lat": str(lat), "lon": str(lon)}]


# --- _overlaps predicate -------------------------------------------------------------------------

def test_overlaps_true_when_bboxes_intersect():
    # NYC_BBOX overlaps a region straddling its western edge.
    assert wc._overlaps((40.50, -74.50, 40.90, -73.90), wc.NYC_BBOX) is True


def test_overlaps_true_when_one_contains_the_other():
    inner = (40.60, -74.00, 40.80, -73.90)  # wholly inside NYC_BBOX
    assert wc._overlaps(inner, wc.NYC_BBOX) is True
    assert wc._overlaps(wc.NYC_BBOX, inner) is True


def test_overlaps_false_when_disjoint():
    montana = (45.0, -114.0, 47.0, -110.0)  # nowhere near NYC
    assert wc._overlaps(montana, wc.NYC_BBOX) is False


def test_overlaps_false_just_west_of_nyc():
    # East edge of the other box (-73.66) is just west of NYC_BBOX's west edge (-73.65 is east).
    # NYC_BBOX west = -74.30; pick a box entirely west of it.
    west = (40.50, -75.00, 40.90, -74.40)  # east edge -74.40 < NYC west -74.30 => disjoint in lon
    assert wc._overlaps(west, wc.NYC_BBOX) is False


@settings(max_examples=200)
@given(
    a=st.tuples(st.floats(-90, 0), st.floats(-180, 0), st.floats(0, 90), st.floats(0, 180)),
    b=st.tuples(st.floats(-90, 0), st.floats(-180, 0), st.floats(0, 90), st.floats(0, 180)),
)
def test_overlaps_is_symmetric(a, b):
    # The bbox-intersection test is symmetric: overlap(a,b) == overlap(b,a). (lat ranges built so
    # south<=north and west<=east, matching how regions are constructed.)
    assert wc._overlaps(a, b) == wc._overlaps(b, a)


@settings(max_examples=200)
@given(
    s=st.floats(35, 45), w=st.floats(-90, -70),
    dlat=st.floats(0.1, 5.0), dlon=st.floats(0.1, 5.0),
)
def test_overlapping_box_implies_contains_center(s, w, dlat, dlon):
    # A box overlapping NYC_BBOX is a necessary condition for any of its points to be in NYC_BBOX.
    # Invariant: if NYC contains the box's own center, the boxes must overlap.
    n, e = s + dlat, w + dlon
    box = (s, w, n, e)
    clat, clon = (s + n) / 2, (w + e) / 2
    s2, w2, n2, e2 = wc.NYC_BBOX
    center_in_nyc = s2 <= clat <= n2 and w2 <= clon <= e2
    if center_in_nyc:
        assert wc._overlaps(box, wc.NYC_BBOX)


# --- fetch(): non-NYC short-circuit --------------------------------------------------------------

def test_non_nyc_region_yields_nothing_and_never_geocodes(monkeypatch):
    client = _FakeClient(_all_to(NYC_LAT, NYC_LON))
    _patch_client(monkeypatch, client)
    montana = Region("mt_t", (45.0, -114.0, 47.0, -110.0), (46.0, -112.0), [], 150)

    scraper = wc.WearableCollectionsScraper()
    recs = list(scraper.fetch(montana))

    assert recs == []
    # The coverage guard must fire BEFORE the client is ever used: no query attempted.
    assert client.queries == []
    assert scraper.fetch_failures == 0


# --- fetch(): happy path -------------------------------------------------------------------------

def test_all_sites_yielded_as_records_for_nyc(monkeypatch):
    client = _FakeClient(_all_to(NYC_LAT, NYC_LON))
    _patch_client(monkeypatch, client)

    scraper = wc.WearableCollectionsScraper()
    recs = list(scraper.fetch(_nyc_region()))

    # Every known SITE geocodes inside NYC -> one record each.
    assert len(recs) == len(wc.SITES)
    assert {r.source_ref for r in recs} == {name for name, _ in wc.SITES}
    # Each site's query was actually geocoded.
    assert client.queries == [query for _, query in wc.SITES]
    assert scraper.fetch_failures == 0


def test_record_shape_matches_source(monkeypatch):
    client = _FakeClient(_all_to(NYC_LAT, NYC_LON))
    _patch_client(monkeypatch, client)

    rec = next(iter(wc.WearableCollectionsScraper().fetch(_nyc_region())))

    assert rec.org_type == "drop_bin"
    assert rec.org_name == "Wearable Collections"
    assert rec.accepted_items == ["clothing", "shoes", "textiles"]
    assert rec.lat == NYC_LAT and rec.lon == NYC_LON
    # source_ref and name are both the site name (first SITES entry).
    assert rec.source_ref == rec.name == wc.SITES[0][0]


def test_geocode_casts_string_coords_to_float(monkeypatch):
    # Nominatim returns lat/lon as strings; the yielded record must carry floats.
    client = _FakeClient(lambda u, p: [{"lat": "40.730000", "lon": "-73.990000"}])
    _patch_client(monkeypatch, client)

    rec = next(iter(wc.WearableCollectionsScraper().fetch(_nyc_region())))

    assert isinstance(rec.lat, float) and isinstance(rec.lon, float)
    assert rec.lat == 40.73 and rec.lon == -73.99


# --- fetch(): unplaceable site bumps fetch_failures ----------------------------------------------

def test_empty_geocode_increments_fetch_failures_and_skips(monkeypatch):
    # Nominatim returns [] for every site -> each is unplaceable.
    client = _FakeClient(lambda u, p: [])
    _patch_client(monkeypatch, client)

    scraper = wc.WearableCollectionsScraper()
    recs = list(scraper.fetch(_nyc_region()))

    assert recs == []
    # One swallowed failure per site -> seen is incomplete; loader must not closure-retire.
    assert scraper.fetch_failures == len(wc.SITES)


def test_one_unplaceable_site_among_good_ones(monkeypatch):
    # First site fails to geocode ([]), the rest land in NYC.
    bad_query = wc.SITES[0][1]

    def responder(url, params):
        if params.get("q") == bad_query:
            return []
        return [{"lat": str(NYC_LAT), "lon": str(NYC_LON)}]

    client = _FakeClient(responder)
    _patch_client(monkeypatch, client)

    scraper = wc.WearableCollectionsScraper()
    recs = list(scraper.fetch(_nyc_region()))

    assert scraper.fetch_failures == 1
    assert len(recs) == len(wc.SITES) - 1
    assert wc.SITES[0][0] not in {r.source_ref for r in recs}


# --- fetch(): in-NYC point outside the region bbox+margin is filtered -----------------------------

def test_in_nyc_point_outside_region_is_filtered_not_failed(monkeypatch):
    # A tiny region INSIDE NYC_BBOX (so the coverage guard passes), but the geocoded point lands
    # well outside it even after the 0.1 deg contains() margin -> filtered, NOT a fetch_failure.
    small = _nyc_region(bbox=(40.70, -74.00, 40.72, -73.98))  # ~0.02 deg box near (40.71, -73.99)
    geo_lat, geo_lon = 40.95, -73.70  # ~0.23 deg NE of the box's north/east edges -> beyond margin
    # Sanity: the point is still inside NYC_BBOX so the guard does pass.
    assert wc._overlaps(small.bbox, wc.NYC_BBOX)
    assert not small.contains(geo_lat, geo_lon, margin=0.1)

    client = _FakeClient(_all_to(geo_lat, geo_lon))
    _patch_client(monkeypatch, client)

    scraper = wc.WearableCollectionsScraper()
    recs = list(scraper.fetch(small))

    assert recs == []                       # every site filtered out by contains()
    assert scraper.fetch_failures == 0      # filtered != unplaceable; no failure bump
    assert client.queries == [q for _, q in wc.SITES]  # but each WAS geocoded


def test_point_just_inside_margin_is_kept(monkeypatch):
    # Control: a point just past the bbox edge but WITHIN the 0.1 deg margin is kept.
    small = _nyc_region(bbox=(40.70, -74.00, 40.72, -73.98))
    geo_lat, geo_lon = 40.75, -73.99  # 0.03 deg north of n=40.72, inside margin; lon inside bbox
    assert not small.contains(geo_lat, geo_lon, margin=0.0)
    assert small.contains(geo_lat, geo_lon, margin=0.1)

    client = _FakeClient(_all_to(geo_lat, geo_lon))
    _patch_client(monkeypatch, client)

    recs = list(wc.WearableCollectionsScraper().fetch(small))

    assert len(recs) == len(wc.SITES)


if __name__ == "__main__":
    print("Run with: PYTHONPATH=. pytest tests/test_wearable_collections.py")
