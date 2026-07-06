"""Density-engine (B8) tests for the /locations cluster tiers.

The map passes its zoom `z`; from it the endpoint picks one bubble per STATE (wide views) or a
ZOOM-AWARE grid (closer in). A client that sends no `z` keeps the legacy bbox_span/32 grid so a
deploy can't regress. See routers/locations.py.
"""
import uuid

from app.routers.locations import (
    CLUSTER_TARGET_PX,
    STATE_BAND_MAX_Z,
    cluster_cell_deg,
)
from conftest import requires_db

# An empty corner of the plains — no seed/fixture data lands here, so the cluster queries see only
# what each test inserts.
_BBOX = "-102.0,44.0,-99.0,47.0"


def _mk_active(conn, lat, lon, state, name="dot"):
    """Insert a single ACTIVE, high-confidence location at a fixed point + state code."""
    conn.execute(
        "INSERT INTO locations (geom, name, org_type, state, status, confidence) "
        "VALUES (ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, 'charity_store', %s, 'active', 90)",
        (lon, lat, f"{name}-{uuid.uuid4()}", state),
    )


def _seed(conn):
    """3 pins in state AA (coincident), 2 in BB, 1 with NO state — 6 total, 3 distinct positions."""
    for _ in range(3):
        _mk_active(conn, 45.0, -100.0, "AA")
    for _ in range(2):
        _mk_active(conn, 46.0, -101.0, "BB")
    _mk_active(conn, 45.5, -100.5, None)  # the ~0.4% real-world null-state case
    conn.commit()


# ---- cluster_cell_deg: the pure slippy-tile cell math ----------------------------------------

def test_cluster_cell_deg_shrinks_with_zoom():
    """Constant on-screen density means the DEGREE cell must halve for every whole zoom step."""
    for zoom in range(2, 12):
        assert cluster_cell_deg(zoom) > cluster_cell_deg(zoom + 1) > 0
        # each whole step halves the world px width, so the degree cell halves too
        assert abs(cluster_cell_deg(zoom) / cluster_cell_deg(zoom + 1) - 2.0) < 1e-9


def test_cluster_cell_deg_matches_slippy_formula():
    # z=8: 360 / 256 * (82/256) = 0.4504°
    assert abs(cluster_cell_deg(8) - (360.0 / 256 * (CLUSTER_TARGET_PX / 256.0))) < 1e-9


def test_cluster_cell_deg_floored_at_deep_zoom():
    """Deep zoom can't request a degenerate sub-metre cell (grid mode won't run there anyway)."""
    assert cluster_cell_deg(30) == 0.005


# ---- state band: one bubble per state, null-state pins preserved ------------------------------

@requires_db
def test_state_band_one_bubble_per_state(client, conn):
    _seed(conn)
    r = client.get("/api/locations", params={"bbox": _BBOX, "cluster": "on", "z": 5})
    body = r.json()
    assert body["mode"] == "clusters" and body["tier"] == "state"
    counts = sorted(c["count"] for c in body["clusters"])
    # AA(3), BB(2), null-state(1) => three bubbles, every pin represented, none dropped
    assert counts == [1, 2, 3]
    assert sum(counts) == 6


@requires_db
def test_state_band_places_bubble_at_state_centroid(client, conn):
    _seed(conn)
    body = client.get("/api/locations", params={"bbox": _BBOX, "cluster": "on", "z": 5}).json()
    aa = next(c for c in body["clusters"] if c["count"] == 3)
    # all three AA pins sit at (45.0, -100.0), so their centroid is exactly there
    assert abs(aa["lat"] - 45.0) < 1e-6 and abs(aa["lon"] - (-100.0)) < 1e-6


# ---- zoom-aware grid: de-overlapped vertices, state-agnostic ----------------------------------

@requires_db
def test_grid_tier_above_state_band(client, conn):
    _seed(conn)
    body = client.get("/api/locations", params={"bbox": _BBOX, "cluster": "on", "z": 9}).json()
    assert body["tier"] == "grid"
    counts = sorted(c["count"] for c in body["clusters"])
    # the grid ignores state: 3 distinct positions -> 3 bubbles (the coincident AA pins share a cell)
    assert counts == [1, 2, 3] and sum(counts) == 6


@requires_db
def test_grid_snaps_bubbles_to_grid_vertices(client, conn):
    """Grid bubbles sit on grid vertices (multiples of the cell), NOT the data centroid — that even
    spacing is what keeps them from overlapping once the frontend caps diameter below the cell."""
    _seed(conn)
    cell = cluster_cell_deg(9)
    body = client.get("/api/locations", params={"bbox": _BBOX, "cluster": "on", "z": 9}).json()
    for c in body["clusters"]:
        # every returned point is an exact multiple of the cell in both axes
        assert abs(c["lon"] / cell - round(c["lon"] / cell)) < 1e-6
        assert abs(c["lat"] / cell - round(c["lat"] / cell)) < 1e-6


@requires_db
def test_tier_boundary_at_state_band_max_z(client, conn):
    _seed(conn)
    at = client.get("/api/locations", params={"bbox": _BBOX, "cluster": "on", "z": STATE_BAND_MAX_Z}).json()
    above = client.get("/api/locations", params={"bbox": _BBOX, "cluster": "on", "z": STATE_BAND_MAX_Z + 1}).json()
    assert at["tier"] == "state"
    assert above["tier"] == "grid"


# ---- backward compatibility: a z-less client keeps the legacy grid ----------------------------

@requires_db
def test_missing_zoom_falls_back_to_legacy_grid(client, conn):
    _seed(conn)
    body = client.get("/api/locations", params={"bbox": _BBOX, "cluster": "on"}).json()
    assert body["mode"] == "clusters" and body["tier"] == "grid"
    # no rows lost in the legacy path either
    assert sum(c["count"] for c in body["clusters"]) == 6
