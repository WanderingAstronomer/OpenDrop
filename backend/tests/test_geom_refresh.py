"""Canonical-pin provenance on re-scrape (pipeline.store.refresh_location_fields).

Ranked bug #2: refresh_location_fields rewrote every display column from the authoritative ingest
source but never touched locations.geom — so when a source corrected a bin's coordinates on a later
run, the map pin stayed frozen at its insert-time position. The fix tracks the authoritative source's
coordinate onto the pin, but ONLY while no human/consensus has adjusted it:

  * geom IS NOT DISTINCT FROM origin_geom  -> no community correction / operator override in effect
    (both move geom away from the immutable origin anchor). Ingest may track the source, and it
    re-centers origin_geom so the 2 km correction cap follows the source's live position.
  * geom IS DISTINCT FROM origin_geom      -> a human/consensus owns the pin; ingest never clobbers it.

Isolated in an empty US bbox; opendrop_test carries no seeded rows.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from conftest import requires_db  # noqa: E402
from pipeline import store  # noqa: E402

# Every test here drives pipeline store.upsert_source (INSERT ... ON CONFLICT DO UPDATE on
# location_sources) — owner-role work in prod, not the restricted app role. Mark owner_only so the
# CI `-m "not owner_only"` restricted pass deselects them instead of erroring on "permission denied".
pytestmark = [requires_db, pytest.mark.owner_only]

_LON0, _LAT0 = -109.90, 45.10  # Montana wilderness — empty


def _rec(lon, lat, **over):
    d = {"name": "GR bin", "org_type": "drop_bin", "org_name": None, "brand_key": None,
         "address_line": None, "house_number": None, "city": None, "state": None,
         "postal_code": None, "hours": None, "hours_raw": None, "accepted_items": None,
         "phone": None, "website": None, "lat": lat, "lon": lon}
    d.update(over)
    return d


def _geom(conn, loc):
    r = conn.execute(
        "SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat, "
        "ST_X(origin_geom) AS olon, ST_Y(origin_geom) AS olat FROM locations WHERE id=%s", (loc,)
    ).fetchone()
    return r


def _at(g, lon, lat):
    """Assert the live pin sits at (lon, lat) within ~0.1 m."""
    return g["lon"] == pytest.approx(lon, abs=1e-6) and g["lat"] == pytest.approx(lat, abs=1e-6)


def _origin_at(g, lon, lat):
    return g["olon"] == pytest.approx(lon, abs=1e-6) and g["olat"] == pytest.approx(lat, abs=1e-6)


@pytest.fixture()
def loc(conn):
    """A fresh osm-sourced location at (_LON0,_LAT0); cleaned up after."""
    d = _rec(_LON0, _LAT0)
    lid = store.insert_location(conn, d)
    store.upsert_source(conn, lid, "osm", "GR-1", _LON0, _LAT0, d["name"], d)
    store.refresh_location_fields(conn, lid)
    conn.commit()
    yield lid
    conn.execute("DELETE FROM location_sources WHERE location_id=%s", (lid,))
    conn.execute("DELETE FROM locations WHERE id=%s", (lid,))
    conn.commit()


def test_rescrape_tracks_source_relocation(conn, loc):
    """The source corrects the bin's coordinate ~150 m east on a later run -> the pin follows, and
    the origin anchor re-centers (no human has touched the pin)."""
    g0 = _geom(conn, loc)
    assert _at(g0, _LON0, _LAT0)

    new_lon, new_lat = _LON0 + 0.002, _LAT0  # source moved
    store.upsert_source(conn, loc, "osm", "GR-1", new_lon, new_lat, "GR bin", _rec(new_lon, new_lat))
    store.refresh_location_fields(conn, loc)
    conn.commit()

    g1 = _geom(conn, loc)
    assert _at(g1, new_lon, new_lat)         # pin tracked the source
    assert _origin_at(g1, new_lon, new_lat)  # anchor re-centered


def test_rescrape_never_clobbers_community_correction(conn, loc):
    """A community correction moved the pin (geom diverges from origin_geom). A later re-scrape must
    NOT drag it back to the source coordinate — the community owns the pin."""
    corrected_lon, corrected_lat = _LON0 + 0.001, _LAT0 + 0.001
    conn.execute(
        "UPDATE locations SET geom = ST_SetSRID(ST_MakePoint(%s,%s),4326) WHERE id=%s",
        (corrected_lon, corrected_lat, loc),
    )  # origin_geom stays at (_LON0,_LAT0) — exactly what an applied correction does
    conn.commit()

    # Source re-reports its own (different) coordinate; ingest must defer to the human-owned pin.
    store.upsert_source(conn, loc, "osm", "GR-1", _LON0 + 0.003, _LAT0, "GR bin",
                        _rec(_LON0 + 0.003, _LAT0))
    store.refresh_location_fields(conn, loc)
    conn.commit()

    g = _geom(conn, loc)
    assert _at(g, corrected_lon, corrected_lat)  # untouched by ingest
    assert _origin_at(g, _LON0, _LAT0)           # origin anchor preserved


def test_rescrape_noop_when_source_unchanged(conn, loc):
    """Idempotence: re-running the loader with the same source coordinate doesn't move the pin."""
    store.upsert_source(conn, loc, "osm", "GR-1", _LON0, _LAT0, "GR bin", _rec(_LON0, _LAT0))
    store.refresh_location_fields(conn, loc)
    conn.commit()
    g = _geom(conn, loc)
    assert _at(g, _LON0, _LAT0)
    assert _origin_at(g, _LON0, _LAT0)
