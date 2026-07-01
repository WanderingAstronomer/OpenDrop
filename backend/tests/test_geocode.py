"""Nominatim geocode/reverse/search parser tests — NO DB, NO NETWORK.

`app.geocode` wraps Nominatim's HTTP API behind three async helpers (search / reverse /
geocode). They are documented as "never raises": a bad response, a transport error, or a
malformed payload must degrade to None / [] rather than blow up a request handler. These
tests pin the exact parse shapes (the JSON keys, the ISO3166-2 state slice, the
boundingbox -> {south,north,west,east} mapping) and the graceful-failure contract.

The network is faked by monkeypatching `geocode.httpx.AsyncClient` with an async
context-manager stub whose `.get()` returns a canned response — same shape the real
httpx client exposes (.raise_for_status(), .json()). Nothing leaves the process.

Run: PYTHONPATH=.:backend pytest backend/tests/test_geocode.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))              # make `pipeline` importable
sys.path.insert(0, str(ROOT / "backend"))  # make `app` importable

from app import geocode  # noqa: E402

try:
    from hypothesis import HealthCheck, given, settings as hyp_settings
    from hypothesis import strategies as st
    _HAS_HYP = True
except Exception:  # pragma: no cover - hypothesis is a dev dep
    _HAS_HYP = False


# --------------------------------------------------------------------------- fakes
class _FakeResp:
    """Mimics an httpx.Response for the two methods the module calls."""
    def __init__(self, payload, raise_exc=None):
        self._payload = payload
        self._raise = raise_exc

    def raise_for_status(self):
        if self._raise is not None:
            raise self._raise

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeClient:
    """Async context-manager HTTP client returning one canned response per get()."""
    def __init__(self, *, payload=None, resp=None, get_exc=None):
        self._resp = resp if resp is not None else _FakeResp(payload)
        self._get_exc = get_exc
        self.calls = []  # captured (url, params) for assertions

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.calls.append((url, params))
        if self._get_exc is not None:
            raise self._get_exc
        return self._resp


def _install(monkeypatch, **kwargs):
    """Patch httpx.AsyncClient(...) -> a fresh _FakeClient; return a holder for inspection."""
    holder = {}

    def factory(*a, **k):
        client = _FakeClient(**kwargs)
        holder["client"] = client
        return client

    monkeypatch.setattr(geocode.httpx, "AsyncClient", factory)
    return holder


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- reverse()
def _reverse_payload():
    """A representative Nominatim jsonv2 reverse response with a full address block."""
    return {
        "display_name": "123 Main Street, Columbus, Franklin County, Ohio, 43004, United States",
        "address": {
            "house_number": "123",
            "road": "Main Street",
            "city": "Columbus",
            "county": "Franklin County",
            "state": "Ohio",
            "ISO3166-2-lvl4": "US-OH",
            "postcode": "43004",
        },
    }


def test_reverse_parses_full_address(monkeypatch):
    _install(monkeypatch, payload=_reverse_payload())
    out = _run(geocode.reverse(40.0, -83.0))
    assert out == {
        "line": "123 Main Street",            # house_number + road, space-joined
        "city": "Columbus",                   # city wins over town/village/county
        "state": "OH",                        # ISO3166-2-lvl4 "US-OH" -> "OH"
        "postal_code": "43004",
        "display_name": _reverse_payload()["display_name"],
    }


def test_reverse_city_falls_back_through_chain(monkeypatch):
    """No city/town/village/hamlet/suburb -> county is used as the city fallback."""
    payload = {
        "display_name": "Rural Route",
        "address": {"county": "Pickaway County", "ISO3166-2-lvl4": "US-OH"},
    }
    _install(monkeypatch, payload=payload)
    out = _run(geocode.reverse(39.5, -83.0))
    assert out["city"] == "Pickaway County"
    assert out["line"] is None       # no house_number and no road key -> None
    assert out["state"] == "OH"


def test_reverse_road_alternates_and_no_house_number(monkeypatch):
    """road key absent -> pedestrian/footway/path are tried; line is road-only w/o number."""
    payload = {
        "display_name": "Walkway",
        "address": {"pedestrian": "Riverside Walk", "town": "Dublin", "ISO3166-2-lvl4": "US-OH"},
    }
    _install(monkeypatch, payload=payload)
    out = _run(geocode.reverse(40.1, -83.1))
    assert out["line"] == "Riverside Walk"   # no house_number -> just the road part
    assert out["city"] == "Dublin"           # town used when city missing


def test_reverse_bad_iso_state_is_none(monkeypatch):
    """A non US-XX ISO value (or wrong length) must not produce a bogus state code."""
    payload = {"display_name": "Somewhere", "address": {"city": "Nowhere", "ISO3166-2-lvl4": "US-OHX"}}
    _install(monkeypatch, payload=payload)
    out = _run(geocode.reverse(40.0, -83.0))
    assert out["state"] is None              # "OHX" is 3 chars -> rejected


def test_reverse_no_address_block_returns_none(monkeypatch):
    """jsonv2 with no 'address' dict -> None (cannot back-fill an address)."""
    _install(monkeypatch, payload={"display_name": "x"})
    assert _run(geocode.reverse(40.0, -83.0)) is None


def test_reverse_non_dict_payload_returns_none(monkeypatch):
    """Nominatim sometimes returns an error list/array -> not a dict -> None."""
    _install(monkeypatch, payload=[])
    assert _run(geocode.reverse(40.0, -83.0)) is None


def test_reverse_http_error_returns_none(monkeypatch):
    """raise_for_status() raising must be swallowed -> None, never propagates."""
    _install(monkeypatch, resp=_FakeResp(None, raise_exc=RuntimeError("503")))
    assert _run(geocode.reverse(40.0, -83.0)) is None


def test_reverse_transport_error_returns_none(monkeypatch):
    """A connect/transport failure at get() time is swallowed -> None."""
    _install(monkeypatch, get_exc=ConnectionError("dns fail"))
    assert _run(geocode.reverse(40.0, -83.0)) is None


def test_reverse_hits_reverse_endpoint(monkeypatch):
    """The /search base url is rewritten to /reverse for reverse-geocoding."""
    holder = _install(monkeypatch, payload=_reverse_payload())
    _run(geocode.reverse(40.0, -83.0))
    url, params = holder["client"].calls[0]
    assert url.endswith("/reverse")
    assert params["lat"] == "40.0" and params["lon"] == "-83.0"
    assert params["format"] == "jsonv2"


# --------------------------------------------------------------------------- search()
def _search_hit(name="Columbus, Ohio", lat="40.0", lon="-83.0", bbox=None):
    d = {"display_name": name, "lat": lat, "lon": lon}
    if bbox is not None:
        d["boundingbox"] = bbox
    return d


def test_search_parses_hits_and_bbox(monkeypatch):
    # Nominatim boundingbox order: [south, north, west, east]
    payload = [_search_hit(bbox=["39.8", "40.2", "-83.2", "-82.8"])]
    _install(monkeypatch, payload=payload)
    out = _run(geocode.search("columbus oh unique-1"))
    assert out == [{
        "name": "Columbus, Ohio",
        "lat": 40.0,
        "lon": -83.0,
        "bbox": {"south": 39.8, "north": 40.2, "west": -83.2, "east": -82.8},
    }]


def test_search_missing_bbox_is_none(monkeypatch):
    _install(monkeypatch, payload=[_search_hit(bbox=None)])
    out = _run(geocode.search("no-bbox-place unique-2"))
    assert out[0]["bbox"] is None
    assert out[0]["lat"] == 40.0 and out[0]["lon"] == -83.0


def test_search_skips_unparseable_rows(monkeypatch):
    """A row whose lat/lon can't float() is skipped; valid siblings still returned."""
    payload = [
        _search_hit(name="bad", lat="not-a-number", lon="-83.0"),
        _search_hit(name="good", lat="41.0", lon="-81.0"),
    ]
    _install(monkeypatch, payload=payload)
    out = _run(geocode.search("mixed rows unique-3"))
    assert [h["name"] for h in out] == ["good"]   # the bad row dropped, good kept


def test_search_empty_result_returns_empty_list(monkeypatch):
    _install(monkeypatch, payload=[])
    assert _run(geocode.search("nowhere unique-4")) == []


def test_search_http_error_returns_empty_list(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(None, raise_exc=RuntimeError("429")))
    assert _run(geocode.search("rate-limited unique-5")) == []


def test_search_uses_cache_second_call(monkeypatch):
    """A second identical query is served from the in-process cache (no get())."""
    q = "cache me unique-6"
    holder1 = _install(monkeypatch, payload=[_search_hit()])
    first = _run(geocode.search(q))
    assert holder1["client"].calls  # network was hit the first time

    # Second call: install a client that would EXPLODE if used; cache must short-circuit it.
    holder2 = _install(monkeypatch, get_exc=AssertionError("cache miss -> network hit"))
    second = _run(geocode.search("  CACHE ME UNIQUE-6 "))   # different case/space, same key
    assert second == first
    assert "client" not in holder2   # factory never invoked -> zero network for cached key


# --------------------------------------------------------------------------- geocode()
def test_geocode_parses_first_hit(monkeypatch):
    _install(monkeypatch, payload=[{"lat": "40.5", "lon": "-82.5"}, {"lat": "1.0", "lon": "1.0"}])
    assert _run(geocode.geocode(line="1 Main St", city="Columbus", state="OH")) == (40.5, -82.5)


def test_geocode_no_fields_short_circuits_without_network(monkeypatch):
    """All-None args => only the 3 constant params => return None and never call get()."""
    holder = _install(monkeypatch, get_exc=AssertionError("should not hit network"))
    assert _run(geocode.geocode()) is None
    assert "client" not in holder  # factory never even invoked


def test_geocode_empty_result_returns_none(monkeypatch):
    _install(monkeypatch, payload=[])
    assert _run(geocode.geocode(postal_code="43004")) is None


def test_geocode_malformed_row_returns_none(monkeypatch):
    """First hit missing lat/lon (or non-float) -> None, never raises."""
    _install(monkeypatch, payload=[{"lat": "oops"}])
    assert _run(geocode.geocode(city="Columbus")) is None


def test_geocode_http_error_returns_none(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(None, raise_exc=RuntimeError("500")))
    assert _run(geocode.geocode(postal_code="43004")) is None


def test_geocode_sends_only_provided_fields(monkeypatch):
    holder = _install(monkeypatch, payload=[{"lat": "40.0", "lon": "-83.0"}])
    _run(geocode.geocode(line="10 Oak", postal_code="43004"))
    _, params = holder["client"].calls[0]
    assert params["street"] == "10 Oak"
    assert params["postalcode"] == "43004"
    assert "city" not in params and "state" not in params
    assert params["limit"] == "1" and params["countrycodes"] == "us"


# --------------------------------------------------------------------------- property-based
if _HAS_HYP:
    @hyp_settings(max_examples=80, deadline=None,
                  suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        lat=st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False),
        lon=st.floats(min_value=-180, max_value=180, allow_nan=False, allow_infinity=False),
        st_code=st.sampled_from(["AL", "OH", "CA", "NY", "WY", "TX"]),
    )
    def test_reverse_roundtrips_lat_lon_and_state_invariant(monkeypatch, lat, lon, st_code):
        """INVARIANT: for any well-formed address with ISO 'US-<XX>', reverse() returns a dict
        whose state is exactly the uppercase two-letter code, and the request carries the lat/lon
        we passed in (stringified). Holds across the whole coordinate domain."""
        payload = {
            "display_name": "prop",
            "address": {"house_number": "5", "road": "Prop Rd", "city": "P", "ISO3166-2-lvl4": f"US-{st_code}"},
        }
        holder = _install(monkeypatch, payload=payload)
        out = _run(geocode.reverse(lat, lon))
        assert out is not None
        assert out["state"] == st_code            # parsed state == the code we fed in
        assert out["line"] == "5 Prop Rd"
        _, params = holder["client"].calls[0]
        assert params["lat"] == str(lat) and params["lon"] == str(lon)

    @hyp_settings(max_examples=80, deadline=None,
                  suppress_health_check=[HealthCheck.function_scoped_fixture])
    @given(
        south=st.floats(min_value=-89, max_value=89, allow_nan=False, allow_infinity=False),
        height=st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False),
        west=st.floats(min_value=-179, max_value=179, allow_nan=False, allow_infinity=False),
        width=st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False),
    )
    def test_search_bbox_mapping_invariant(monkeypatch, south, height, west, width):
        """INVARIANT: search() maps Nominatim's [south, north, west, east] array straight into the
        named bbox dict with no reordering or arithmetic. north>south and east>west are preserved."""
        north = south + height
        east = west + width
        bb = [str(south), str(north), str(west), str(east)]
        # unique query each example so the cache can't mask a regression
        q = f"prop {south}:{north}:{west}:{east}"
        _install(monkeypatch, payload=[_search_hit(lat=str(south), lon=str(west), bbox=bb)])
        out = _run(geocode.search(q))
        assert out[0]["bbox"] == {"south": south, "north": north, "west": west, "east": east}
        assert out[0]["bbox"]["north"] > out[0]["bbox"]["south"]
        assert out[0]["bbox"]["east"] > out[0]["bbox"]["west"]


if __name__ == "__main__":
    print("Run with: PYTHONPATH=.:backend pytest backend/tests/test_geocode.py")
