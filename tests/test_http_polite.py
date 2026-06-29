"""Tests for the polite HTTP client (no network, no real sleeping).

PoliteClient is what keeps a nationwide overnight sweep a good citizen, so its retry/backoff/
pacing behavior is pinned here. We drive it with httpx.MockTransport (no sockets), stub out
time.sleep (record, don't wait) and random jitter (deterministic), and — for the pacing test —
a fake monotonic clock.
Run: PYTHONPATH=. pytest tests/test_http_polite.py
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.scrapers import http as ph  # noqa: E402


def _client(handler, **kw):
    """A PoliteClient whose underlying httpx.Client is backed by a scripted MockTransport."""
    c = ph.PoliteClient(**kw)
    c._client = httpx.Client(transport=httpx.MockTransport(handler))
    return c


@pytest.fixture()
def no_wait(monkeypatch):
    """Record backoff sleeps without waiting; zero out jitter for deterministic wait values."""
    slept = []
    monkeypatch.setattr(ph.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(ph.random, "uniform", lambda a, b: 0.0)
    return slept


def test_retries_transient_5xx_then_succeeds(no_wait):
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        return httpx.Response(200 if calls["n"] >= 3 else 503)

    with _client(handler, delay_s=0) as c:
        r = c.get("http://x")
    assert r.status_code == 200
    assert calls["n"] == 3
    # Two backoffs before the 3rd (successful) attempt: BASE*2^0, BASE*2^1 (jitter zeroed).
    assert no_wait == [ph.BACKOFF_BASE_S, ph.BACKOFF_BASE_S * 2]


def test_honors_retry_after_header_on_429(no_wait):
    calls = {"n": 0}

    def handler(_req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200)

    with _client(handler, delay_s=0) as c:
        r = c.get("http://x")
    assert r.status_code == 200
    assert no_wait == [7.0]  # server's hint wins over computed backoff


def test_raises_after_exhausting_retries(no_wait):
    def handler(_req):
        return httpx.Response(500)

    with _client(handler, delay_s=0, max_retries=2) as c:
        with pytest.raises(httpx.HTTPStatusError):
            c.get("http://x")
    # max_retries=2 => 2 backoffs then the 3rd failure re-raises.
    assert no_wait == [ph.BACKOFF_BASE_S, ph.BACKOFF_BASE_S * 2]


def test_non_retryable_4xx_returned_not_retried(no_wait):
    def handler(_req):
        return httpx.Response(404)

    with _client(handler, delay_s=0) as c:
        r = c.get("http://x")
    assert r.status_code == 404
    assert no_wait == []  # a 404 is the caller's to handle, not a transient failure


def test_paces_consecutive_requests(monkeypatch):
    # Fake monotonic clock so we can assert the inter-request gap deterministically.
    ticks = iter([100.0, 100.1, 100.2, 100.3, 100.4])
    monkeypatch.setattr(ph.time, "monotonic", lambda: next(ticks))
    slept = []
    monkeypatch.setattr(ph.time, "sleep", lambda s: slept.append(s))

    with _client(lambda _r: httpx.Response(200), delay_s=0.5) as c:
        c.get("http://x")  # first call: no pacing (no prior request)
        c.get("http://x")  # second call: must wait out the remainder of the 0.5s window
    assert len(slept) == 1
    assert abs(slept[0] - 0.4) < 1e-9  # 0.5 window minus the 0.1 already elapsed


def test_zero_delay_disables_pacing(monkeypatch):
    slept = []
    monkeypatch.setattr(ph.time, "sleep", lambda s: slept.append(s))
    with _client(lambda _r: httpx.Response(200), delay_s=0) as c:
        c.get("http://x")
        c.get("http://x")
    assert slept == []  # delay_s=0 => never paces


if __name__ == "__main__":
    # Minimal self-run without pytest fixtures (skips the fixture-based cases).
    print("Run with: pytest tests/test_http_polite.py")
