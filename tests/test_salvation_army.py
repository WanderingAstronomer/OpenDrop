"""Salvation Army scraper: the ZIP sweep must drop locations that fall OUTSIDE the region bbox.

A ZIP near a region's edge returns stores that physically belong to the neighbouring region; the
sibling ZIP-sweep scrapers (USAgain, Wearable Collections) already filter their yield through
`region.contains(..., margin=0.05)`. This pins that Salvation Army does the same, so a national
run never ingests a store against the wrong region. No DB, no network — the HTTP client is faked.
Run: PYTHONPATH=. pytest tests/test_salvation_army.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.regions import Region  # noqa: E402
from pipeline.scrapers import salvation_army  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeClient:
    """Context-manager HTTP client that returns one canned payload for every ZIP get()."""
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        return _FakeResp(self._payload)


def _loc(guid, lat, lon, name="SA store"):
    return {"LocationGUID": guid, "Latitude": lat, "Longitude": lon, "Name": name,
            "TypeName": "STORE", "City": "X", "State": "OH", "Zip": "43004"}


def _payload(locs):
    return {"RetVal": {"Locations": locs}}


def _region():
    # Compact region around (40.0, -83.0); one ZIP drives a single sweep. The contains() margin is
    # 0.05 deg (~5.5 km), enough tolerance for a store just over the line from a border ZIP.
    return Region("sa_t", (39.90, -83.10, 40.10, -82.90), (40.0, -83.0), ["43004"], 25)


def test_fetch_drops_out_of_region_locations(monkeypatch):
    inside = _loc("in-1", 40.00, -83.00)   # squarely inside the bbox
    border = _loc("in-2", 40.13, -83.00)   # 0.03 deg past the north edge -> within the 0.05 margin
    far = _loc("out-1", 41.50, -83.00)     # ~1.5 deg north -> a neighbouring region's store
    monkeypatch.setattr(salvation_army, "PoliteClient",
                        lambda *a, **k: _FakeClient(_payload([inside, border, far])))

    refs = {r.source_ref for r in salvation_army.SalvationArmyScraper().fetch(_region())}

    assert "in-1" in refs and "in-2" in refs   # inside + border (within margin) are kept
    assert "out-1" not in refs                 # the far-away neighbour is dropped


def test_fetch_keeps_all_in_region(monkeypatch):
    """Control: when every store is inside the bbox, none are dropped (the filter isn't over-eager)."""
    locs = [_loc(f"g{i}", 40.0 + i * 0.01, -83.0) for i in range(5)]  # 40.00..40.04, all inside
    monkeypatch.setattr(salvation_army, "PoliteClient",
                        lambda *a, **k: _FakeClient(_payload(locs)))

    refs = {r.source_ref for r in salvation_army.SalvationArmyScraper().fetch(_region())}

    assert refs == {f"g{i}" for i in range(5)}


if __name__ == "__main__":
    print("Run with: PYTHONPATH=. pytest tests/test_salvation_army.py")
