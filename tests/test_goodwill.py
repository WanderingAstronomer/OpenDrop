"""Goodwill ENRICH-ONLY scraper — nonce harvest + AJAX donation-site parsing (no DB, no network).

The HTTP client is faked: one fake serves the locator HTML (so `_nonce()` can regex a nonce out of
it) and the AJAX JSON body for everything else. These pin the contract the loader relies on:

  * `_nonce()` pulls the hex nonce out of matching locator HTML, and returns None when it's absent
    (=> fetch yields nothing and never reaches the AJAX call);
  * a normal AJAX body yields the donation sites with the exact source_ref/name/lat/lon/org_type the
    parser builds;
  * a non-donation row (ci_servD=0 AND no "Donation" in calcd_ServicesOffered) is filtered out;
  * a row missing lat/lon ("" or None) is skipped;
  * the nested-`data` unwrapping handles both body["data"]["data"] and a flat body["data"] list.

Assertions are derived directly from pipeline/scrapers/goodwill.py (regex, JSON keys, filter logic).
Run: PYTHONPATH=. pytest tests/test_goodwill.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.regions import Region  # noqa: E402
from pipeline.scrapers import goodwill  # noqa: E402

try:
    from hypothesis import given, settings
    from hypothesis import strategies as st
    _HAS_HYPOTHESIS = True
except ImportError:  # pragma: no cover - hypothesis is a dev dep, present in CI
    _HAS_HYPOTHESIS = False


# --- fakes -------------------------------------------------------------------------------------

class _TextResp:
    """Locator response: only `.text` is read by `_nonce()`."""
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _JsonResp:
    """AJAX response: `.raise_for_status()` then `.json()` are called in fetch()."""
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Context-manager client. `get(LOCATOR)` -> locator HTML; any other get -> the AJAX body.

    `locator_html` may be None to simulate a locator fetch that yields no nonce. `raise_on` lets a
    test force an exception from a given URL substring if needed (unused by default)."""
    def __init__(self, locator_html, ajax_payload):
        self._locator_html = locator_html
        self._ajax_payload = ajax_payload
        self.ajax_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        if url == goodwill.LOCATOR:
            return _TextResp(self._locator_html if self._locator_html is not None else "")
        self.ajax_calls.append((url, params))
        return _JsonResp(self._ajax_payload)


def _patch_client(monkeypatch, locator_html, ajax_payload):
    """Monkeypatch the PoliteClient symbol goodwill imports; return the shared fake so a test can
    inspect whether the AJAX endpoint was hit."""
    client = _FakeClient(locator_html, ajax_payload)
    monkeypatch.setattr(goodwill, "PoliteClient", lambda *a, **k: client)
    return client


def _region():
    # fetch() only reads region.center (unpacked lat, lng) and region.radius_mi.
    return Region("gw_t", (39.0, -84.0, 41.0, -82.0), (40.0, -83.0), [], 150)


# A locator page that _NONCE_RE (nonce["\']?\s*[:=]\s*["\']([0-9a-f]{8,})["\']) matches.
LOCATOR_HTML = '<script>window.gwlfGlobal = {nonce: "deadbeef12", foo: 1};</script>'


def _site(loc_id, lat="40.10", lon="-83.10", name="Goodwill A", **over):
    """A donation-eligible row (ci_servD="1") with the exact keys the parser reads."""
    row = {
        "LocationId": loc_id,
        "LocationName": name,
        "LocationLatitude1": lat,
        "LocationLongitude1": lon,
        "ci_servD": "1",
        "calcd_ServicesOffered": "Donation, Retail",
        "LocationStreetAddress1": "1 Main St",
        "LocationCity1": "Columbus",
        "LocationState1": "OH",
        "LocationPostal1": "43004",
        "LocationPhoneOffice": "555-1212",
    }
    row.update(over)
    return row


# --- _nonce() ----------------------------------------------------------------------------------

def test_nonce_extracts_hex_from_matching_html(monkeypatch):
    client = _patch_client(monkeypatch, LOCATOR_HTML, {"data": []})
    assert goodwill.GoodwillScraper._nonce(client) == "deadbeef12"


def test_nonce_none_when_absent(monkeypatch):
    # No "nonce: <hex>" anywhere -> regex misses -> None.
    client = _patch_client(monkeypatch, "<html><body>no token here</body></html>", {"data": []})
    assert goodwill.GoodwillScraper._nonce(client) is None


def test_no_nonce_means_no_records_and_no_ajax(monkeypatch):
    # When the nonce can't be harvested, fetch() returns before ever calling the AJAX endpoint.
    client = _patch_client(monkeypatch, "<html>nothing</html>", {"data": [_site(1)]})
    recs = list(goodwill.GoodwillScraper().fetch(_region()))
    assert recs == []
    assert client.ajax_calls == []  # AJAX endpoint never hit


# --- normal AJAX body --------------------------------------------------------------------------

def test_normal_body_yields_donation_sites(monkeypatch):
    payload = {"data": {"data": [_site(101, lat="40.10", lon="-83.10", name="Goodwill North")]}}
    _patch_client(monkeypatch, LOCATOR_HTML, payload)

    recs = list(goodwill.GoodwillScraper().fetch(_region()))

    assert len(recs) == 1
    r = recs[0]
    assert r.source_ref == "101"          # str(LocationId)
    assert r.name == "Goodwill North"      # LocationName
    assert r.lat == 40.10 and r.lon == -83.10
    assert r.org_type == "donation_center"
    assert r.org_name == "Goodwill"
    assert r.address_line == "1 Main St"
    assert r.city == "Columbus"
    assert r.state == "OH"
    assert r.postal_code == "43004"
    assert r.phone == "555-1212"


def test_ajax_called_with_harvested_nonce(monkeypatch):
    client = _patch_client(monkeypatch, LOCATOR_HTML, {"data": {"data": [_site(1)]}})
    list(goodwill.GoodwillScraper().fetch(_region()))
    assert len(client.ajax_calls) == 1
    url, params = client.ajax_calls[0]
    assert url == goodwill.AJAX
    assert params["security"] == "deadbeef12"   # the harvested nonce is forwarded
    assert params["action"] == "gwlf_get_locations"


def test_source_ref_falls_back_to_name_when_no_id(monkeypatch):
    # str(LocationId or LocationName): missing id -> name is used.
    row = _site(None, name="Goodwill Fallback")
    del row["LocationId"]
    _patch_client(monkeypatch, LOCATOR_HTML, {"data": {"data": [row]}})
    recs = list(goodwill.GoodwillScraper().fetch(_region()))
    assert len(recs) == 1
    assert recs[0].source_ref == "Goodwill Fallback"


# --- filtering ---------------------------------------------------------------------------------

def test_non_donation_row_is_filtered_out(monkeypatch):
    # ci_servD=0 AND no "Donation" in calcd_ServicesOffered -> dropped.
    retail = _site(2, name="Retail Only", ci_servD="0", calcd_ServicesOffered="Retail, Jobs")
    donor = _site(3, name="Donor")
    _patch_client(monkeypatch, LOCATOR_HTML, {"data": {"data": [retail, donor]}})

    refs = {r.source_ref for r in goodwill.GoodwillScraper().fetch(_region())}
    assert refs == {"3"}  # only the donation site survives


def test_donation_via_services_string_is_kept(monkeypatch):
    # ci_servD=0 but "Donation" present in calcd_ServicesOffered -> kept by the OR branch.
    row = _site(4, name="Via Services", ci_servD="0", calcd_ServicesOffered="Retail and Donation")
    _patch_client(monkeypatch, LOCATOR_HTML, {"data": {"data": [row]}})
    refs = {r.source_ref for r in goodwill.GoodwillScraper().fetch(_region())}
    assert refs == {"4"}


def test_rows_missing_latlon_are_skipped(monkeypatch):
    none_lat = _site(5, name="NoLat", lat=None)
    empty_lon = _site(6, name="EmptyLon", lon="")
    good = _site(7, name="Good")
    _patch_client(monkeypatch, LOCATOR_HTML, {"data": {"data": [none_lat, empty_lon, good]}})

    refs = {r.source_ref for r in goodwill.GoodwillScraper().fetch(_region())}
    assert refs == {"7"}  # both coord-less rows dropped, the complete one kept


def test_non_dict_rows_are_ignored(monkeypatch):
    # `if not isinstance(loc, dict): continue` — junk entries don't crash the parse.
    _patch_client(monkeypatch, LOCATOR_HTML, {"data": {"data": ["junk", None, 42, _site(8)]}})
    refs = {r.source_ref for r in goodwill.GoodwillScraper().fetch(_region())}
    assert refs == {"8"}


# --- nested-data unwrapping --------------------------------------------------------------------

def test_unwrap_nested_data_data(monkeypatch):
    # body["data"]["data"] is a list -> used directly.
    _patch_client(monkeypatch, LOCATOR_HTML, {"data": {"data": [_site(10), _site(11)]}})
    refs = {r.source_ref for r in goodwill.GoodwillScraper().fetch(_region())}
    assert refs == {"10", "11"}


def test_unwrap_dict_without_inner_data_key(monkeypatch):
    # body["data"] is a dict carrying the rows under an inner "data" key (alongside other keys).
    # The isinstance(raw, dict) branch unwraps raw.get("data") and ignores the rest.
    _patch_client(monkeypatch, LOCATOR_HTML,
                  {"data": {"data": [_site(30)], "meta": {"count": 1}}})
    refs = {r.source_ref for r in goodwill.GoodwillScraper().fetch(_region())}
    assert refs == {"30"}


def test_flat_nonempty_data_list_yields_records(monkeypatch):
    # A NON-EMPTY flat body["data"] list is now unwrapped directly. Previously the chained
    # `(body.get("data") or {}).get("data")` called `.get` on the list and raised AttributeError
    # straight out of fetch() (the unwrap sits outside the try/except). Both the nested and flat
    # envelope shapes must yield the same records.
    _patch_client(monkeypatch, LOCATOR_HTML, {"data": [_site(20), _site(21)]})
    refs = {r.source_ref for r in goodwill.GoodwillScraper().fetch(_region())}
    assert refs == {"20", "21"}


def test_empty_data_yields_nothing(monkeypatch):
    _patch_client(monkeypatch, LOCATOR_HTML, {"data": []})
    assert list(goodwill.GoodwillScraper().fetch(_region())) == []


# --- property-based: every yielded record satisfies the parser's invariants --------------------

if _HAS_HYPOTHESIS:

    @settings(max_examples=120, deadline=None)
    @given(
        rows=st.lists(
            st.fixed_dictionaries({
                "LocationId": st.integers(min_value=1, max_value=10_000),
                "LocationName": st.text(min_size=1, max_size=12).filter(lambda s: s.strip() != ""),
                "LocationLatitude1": st.floats(min_value=39.0, max_value=41.0,
                                               allow_nan=False, allow_infinity=False),
                "LocationLongitude1": st.floats(min_value=-84.0, max_value=-82.0,
                                                allow_nan=False, allow_infinity=False),
                # donation eligibility toggled both ways so the filter is exercised
                "ci_servD": st.sampled_from(["0", "1"]),
                "calcd_ServicesOffered": st.sampled_from(["Retail", "Retail, Donation", "Donation"]),
            }),
            min_size=0, max_size=8,
        )
    )
    def test_property_every_yielded_record_is_a_donation_site_with_valid_coords(rows):
        # Patch/restore PoliteClient by hand (no function-scoped monkeypatch fixture, which Hypothesis
        # rejects because it isn't reset between generated inputs).
        payload = {"data": {"data": rows}}
        client = _FakeClient(LOCATOR_HTML, payload)
        original = goodwill.PoliteClient
        goodwill.PoliteClient = lambda *a, **k: client
        try:
            recs = list(goodwill.GoodwillScraper().fetch(_region()))
        finally:
            goodwill.PoliteClient = original

        # INVARIANT 1: a record is yielded iff the source row is donation-eligible (the OR filter).
        def eligible(row):
            return str(row.get("ci_servD") or "0") in ("1", "true", "True") or \
                "Donation" in str(row.get("calcd_ServicesOffered") or "")

        expected_refs = [str(row["LocationId"]) for row in rows if eligible(row)]
        assert sorted(r.source_ref for r in recs) == sorted(expected_refs)

        # INVARIANT 2: every yielded record carries the fixed org identity + float coords.
        for r in recs:
            assert r.org_type == "donation_center"
            assert r.org_name == "Goodwill"
            assert isinstance(r.lat, float) and isinstance(r.lon, float)


if __name__ == "__main__":
    print("Run with: PYTHONPATH=. pytest tests/test_goodwill.py")
