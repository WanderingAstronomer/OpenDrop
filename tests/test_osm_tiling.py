"""Tests for OSM national tiling (no DB, no network).

A national region bbox is far too large for one Overpass query, so osm_ingest splits it into
<= OSM_TILE_DEGREES tiles, queries each, and merges — deduping elements that fall in two adjacent
tiles. These pin that split/merge/dedup contract and the fixture-fallback guard (the Columbus
fixture must only stand in for regions that actually cover Columbus).
Run: PYTHONPATH=. pytest tests/test_osm_tiling.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.osm_ingest import (  # noqa: E402
    OsmScraper,
    _covers_fixture,
    _region_bbox,
    _tile_degrees,
    _tiles,
)
from pipeline.regions import REGIONS, Region  # noqa: E402


def test_small_bbox_yields_one_tile():
    bbox = (39.80, -83.25, 40.18, -82.75)  # < 3 deg each way
    assert list(_tiles(bbox, 3.0)) == [bbox]


def test_large_bbox_splits_and_covers():
    bbox = REGIONS["ohio"].bbox  # (38.40, -84.82, 41.98, -80.52): ~3.6 x ~4.3 deg
    tiles = list(_tiles(bbox, 3.0))
    assert len(tiles) == 4  # 2 lat bands x 2 lon bands
    s, w, n, e = bbox
    # Tiles must stay within the parent bbox and collectively span it edge to edge.
    assert all(s <= ts and tn <= n and w <= tw and te <= e for ts, tw, tn, te in tiles)
    assert min(t[0] for t in tiles) == s and max(t[2] for t in tiles) == n
    assert min(t[1] for t in tiles) == w and max(t[3] for t in tiles) == e


def test_tile_count_scales_for_a_wide_region():
    # The synthesized national region must shatter into many tiles, not one giant query.
    usa = _region_bbox(REGIONS["greater_ohio"])
    assert len(list(_tiles(usa, 3.0))) > 1


def test_covers_fixture_guard():
    assert _covers_fixture((39.80, -83.25, 40.18, -82.75))      # Columbus inside
    assert not _covers_fixture((45.0, -114.0, 47.0, -110.0))    # Montana — fixture must NOT apply


def test_tile_degrees_env_override(monkeypatch):
    monkeypatch.setenv("OSM_TILE_DEGREES", "5")
    assert _tile_degrees() == 5.0
    monkeypatch.setenv("OSM_TILE_DEGREES", "garbage")
    assert _tile_degrees() == 3.0       # invalid -> default
    monkeypatch.setenv("OSM_TILE_DEGREES", "0")
    assert _tile_degrees() == 3.0       # non-positive -> default


def _node(nid):
    return {"type": "node", "id": nid, "lat": 40.0, "lon": -83.0,
            "tags": {"shop": "charity", "name": f"n{nid}"}}


def test_fetch_dedupes_elements_across_tiles():
    scraper = OsmScraper()
    region = Region("t", (39.0, -84.0, 41.0, -82.0), (40.0, -83.0), [], 150)
    # Same node appears in two adjacent tiles; a third is distinct.
    scraper._collect = lambda bbox: [_node(1), _node(1), _node(2)]
    recs = list(scraper.fetch(region))
    assert {r.source_ref for r in recs} == {"node/1", "node/2"}


def test_fetch_no_fixture_outside_columbus_when_overpass_dead():
    scraper = OsmScraper()
    montana = Region("mt_t", (45.0, -114.0, 47.0, -110.0), (46.0, -112.0), [], 150)
    scraper._collect = lambda bbox: None   # every tile failed
    # Region doesn't cover the fixture metro, so it must yield NOTHING (no Columbus bins in Montana).
    assert list(scraper.fetch(montana)) == []


if __name__ == "__main__":
    print("Run with: pytest tests/test_osm_tiling.py")
