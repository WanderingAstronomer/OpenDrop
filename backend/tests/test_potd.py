"""Wikimedia Picture-of-the-Day proxy tests — NO DB, NO NETWORK.

`app.routers.potd` fetches today's Commons POTD from the Wikipedia featured feed and reshapes it
into a flat, attribution-complete payload. Contract pinned here:
  * a well-formed feed shapes the payload correctly (the exact key mapping incl. artist/description
    `.text`, and available:true);
  * ANY failure (non-200, transport error, missing/oddly-typed `image`, malformed body) degrades to
    {"available": false} — the frontend renders nothing rather than break.

The network is faked by monkeypatching `potd.httpx.AsyncClient` with an async context-manager stub
whose `.get()` returns a canned response (same _FakeClient shape as test_geocode.py). The
module-level day-cache is reset before every test so cases don't bleed into each other. Nothing
leaves the process.

Run: PYTHONPATH=.:backend pytest backend/tests/test_potd.py
"""
import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))              # make `pipeline` importable
sys.path.insert(0, str(ROOT / "backend"))  # make `app` importable

from app.routers import potd  # noqa: E402


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
        self.calls = []  # captured urls for assertions

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.calls.append(url)
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

    monkeypatch.setattr(potd.httpx, "AsyncClient", factory)
    return holder


@pytest.fixture(autouse=True)
def _reset_cache():
    """The day-cache is module global — clear it so each test starts from a cold fetch."""
    potd._cache = None
    yield
    potd._cache = None


def _run(coro):
    return asyncio.run(coro)


def _good_feed():
    """A representative Wikipedia featured-feed response with a full POTD `image` block."""
    return {
        "image": {
            "title": "File:Sunset over the sea.jpg",
            "image": {"source": "https://upload.wikimedia.org/full/Sunset.jpg"},
            "thumbnail": {"source": "https://upload.wikimedia.org/thumb/Sunset.jpg"},
            "file_page": "https://commons.wikimedia.org/wiki/File:Sunset_over_the_sea.jpg",
            "artist": {"text": "Jane Photographer", "html": "<a href='#'>Jane Photographer</a>"},
            "license": {"type": "CC BY-SA 4.0", "url": "https://creativecommons.org/licenses/by-sa/4.0/"},
            "description": {"text": "A sunset over the sea.", "html": "<p>A sunset over the sea.</p>"},
        },
    }


# --------------------------------------------------------------------------- happy path
def test_good_feed_shapes_payload(monkeypatch):
    _install(monkeypatch, payload=_good_feed())
    out = _run(potd.potd())
    assert out["available"] is True
    assert out["title"] == "File:Sunset over the sea.jpg"
    assert out["image_url"] == "https://upload.wikimedia.org/full/Sunset.jpg"
    assert out["thumb_url"] == "https://upload.wikimedia.org/thumb/Sunset.jpg"
    assert out["source_url"] == "https://commons.wikimedia.org/wiki/File:Sunset_over_the_sea.jpg"
    # artist/description come from `.text` (never the `.html`) so no upstream markup is injected.
    assert out["artist"] == "Jane Photographer"
    assert out["description"] == "A sunset over the sea."
    assert out["license"] == "CC BY-SA 4.0"
    assert out["license_url"] == "https://creativecommons.org/licenses/by-sa/4.0/"
    assert out["date"] and out["date"].count("-") == 2  # YYYY-MM-DD


def test_partial_image_block_still_available_with_none_gaps(monkeypatch):
    """A POTD missing some optional fields is still available; absent keys map to None, not crash."""
    _install(monkeypatch, payload={"image": {"title": "File:x.jpg",
                                             "image": {"source": "https://upload.wikimedia.org/x.jpg"}}})
    out = _run(potd.potd())
    assert out["available"] is True
    assert out["image_url"] == "https://upload.wikimedia.org/x.jpg"
    assert out["artist"] is None and out["license"] is None and out["thumb_url"] is None


def test_hits_todays_feed_url(monkeypatch):
    holder = _install(monkeypatch, payload=_good_feed())
    _run(potd.potd())
    url = holder["client"].calls[0]
    assert url.startswith("https://en.wikipedia.org/api/rest_v1/feed/featured/")


# --------------------------------------------------------------------------- graceful failure
def test_missing_image_block_unavailable(monkeypatch):
    """A feed with no `image` (e.g. the field simply absent) -> available:false, HTTP-200 shape."""
    _install(monkeypatch, payload={"tfa": {"title": "some article"}})  # no POTD today
    assert _run(potd.potd()) == {"available": False}


def test_image_not_a_dict_unavailable(monkeypatch):
    _install(monkeypatch, payload={"image": "oops-a-string"})
    assert _run(potd.potd()) == {"available": False}


def test_non_dict_payload_unavailable(monkeypatch):
    _install(monkeypatch, payload=[])  # array/error body, not the expected object
    assert _run(potd.potd()) == {"available": False}


def test_http_error_unavailable(monkeypatch):
    """raise_for_status() raising (non-200) must be swallowed -> available:false."""
    _install(monkeypatch, resp=_FakeResp(None, raise_exc=RuntimeError("503")))
    assert _run(potd.potd()) == {"available": False}


def test_transport_error_unavailable(monkeypatch):
    """A connect/transport failure at get() time is swallowed -> available:false."""
    _install(monkeypatch, get_exc=ConnectionError("dns fail"))
    assert _run(potd.potd()) == {"available": False}


def test_malformed_json_body_unavailable(monkeypatch):
    """.json() itself raising (truncated/garbage body) still degrades cleanly."""
    _install(monkeypatch, payload=ValueError("not json"))
    assert _run(potd.potd()) == {"available": False}


# --------------------------------------------------------------------------- caching
def test_same_day_is_served_from_cache(monkeypatch):
    """A second same-day call must NOT hit the network — the first response is cached for the day."""
    holder1 = _install(monkeypatch, payload=_good_feed())
    first = _run(potd.potd())
    assert holder1["client"].calls  # network hit the first time

    # Install a client that EXPLODES if used; the day-cache must short-circuit it.
    holder2 = _install(monkeypatch, get_exc=AssertionError("cache miss -> network hit"))
    second = _run(potd.potd())
    assert second == first
    assert "client" not in holder2  # factory never invoked -> zero network on the cached day


def test_date_rollover_refetches(monkeypatch):
    """When the cached date is stale (UTC day rolled over), the next call refetches."""
    _install(monkeypatch, payload=_good_feed())
    _run(potd.potd())
    potd._cache["date"] = "1999-01-01"  # pretend the cached entry is from a previous day

    holder2 = _install(monkeypatch, payload=_good_feed())
    _run(potd.potd())
    assert holder2["client"].calls  # a stale date forces a fresh fetch


def test_negative_result_refetches_after_ttl(monkeypatch):
    """A cached MISS is only trusted for the negative TTL; past it, the next call retries upstream
    (so a transient outage self-heals mid-day instead of pinning 'unavailable')."""
    _install(monkeypatch, get_exc=ConnectionError("blip"))
    assert _run(potd.potd()) == {"available": False}
    # Age the negative cache entry beyond its TTL.
    potd._cache["at"] -= (potd._NEGATIVE_TTL_S + 1)

    holder2 = _install(monkeypatch, payload=_good_feed())
    out = _run(potd.potd())
    assert out["available"] is True  # recovered — the stale miss did not stick
    assert holder2["client"].calls


if __name__ == "__main__":
    print("Run with: PYTHONPATH=.:backend pytest backend/tests/test_potd.py")
