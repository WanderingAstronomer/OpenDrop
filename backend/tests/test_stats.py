"""Operator-only /admin/stats snapshot:

  * invisible without a valid X-Operator-Token — 404, not 401/403, like every /admin route;
  * with the token, a 200 whose counts reflect exactly the rows present.

DB-backed. The autouse _isolate_db fixture truncates the mutable tables before each test, so every
assertion starts from a known slate (the `sources` registry is preserved, so the source_code FK on
an inserted location_sources row resolves)."""
import uuid

import pytest
from conftest import requires_db

OP_TOKEN = "test-operator-secret-0123456789"


@pytest.fixture()
def op():
    from app.config import settings
    prev = settings.operator_token
    settings.operator_token = OP_TOKEN
    yield {"X-Operator-Token": OP_TOKEN}
    settings.operator_token = prev


def _mk_location(conn, name, source="planet_aid", org_type="drop_bin", status="active",
                 state="OH", lat=40.1, lon=-83.1):
    loc_id = conn.execute(
        "INSERT INTO locations (geom, name, org_type, status, state, confidence) "
        "VALUES (ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, %s, %s::location_status, %s, 60) RETURNING id",
        (lon, lat, name, org_type, status, state),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO location_sources (location_id, source_code, source_ref, source_geom) "
        "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s,%s),4326))",
        (loc_id, source, f"{source}/{uuid.uuid4()}", lon, lat))
    conn.commit()
    return loc_id


@requires_db
def test_stats_is_invisible_without_operator_token(client):
    # No token / wrong token -> 404 (not 401/403): the surface is invisible to probes.
    assert client.get("/api/admin/stats").status_code == 404
    assert client.get("/api/admin/stats", headers={"X-Operator-Token": "wrong"}).status_code == 404


@requires_db
def test_stats_counts_reflect_rows(client, op, conn):
    # Three sourced bins (a confidence trigger auto-promotes a sourced bin to 'active')...
    _mk_location(conn, "A", source="planet_aid", state="OH")
    _mk_location(conn, "B", source="planet_aid", state="OH")
    _mk_location(conn, "C", source="salvation_army", state="MD")
    # ...plus one source-less, zero-confidence row that stays 'pending' (no promotion trigger fires
    # without a source) — so the status GROUP BY is exercised across more than one status.
    conn.execute(
        "INSERT INTO locations (geom, name, org_type, status, state, confidence) "
        "VALUES (ST_SetSRID(ST_MakePoint(-83.0,40.0),4326), 'D', 'drop_bin', 'pending'::location_status, 'OH', 0)")
    conn.commit()

    # The DB has confidence->status (and redistributable) triggers, so derive the trigger-sensitive
    # expectations from the DB and assert the endpoint mirrors them faithfully.
    exp = conn.execute(
        "SELECT count(*) AS total, "
        "count(*) FILTER (WHERE status='active') AS active, "
        "count(*) FILTER (WHERE status='pending') AS pending, "
        "count(*) FILTER (WHERE status='active' AND is_redistributable) AS public, "
        "count(DISTINCT state) FILTER (WHERE status='active' AND state IS NOT NULL) AS states "
        "FROM locations"
    ).fetchone()

    r = client.get("/api/admin/stats", headers=op)
    assert r.status_code == 200
    s = r.json()

    # trigger-independent — reflect exactly what we inserted
    assert s["locations"]["total"] == 4 == exp["total"]
    assert s["sources"]["links_by_source"] == {"planet_aid": 2, "salvation_army": 1}
    assert s["recent"]["new_locations"]["d7"] == 4                  # all just inserted
    # trigger-sensitive — endpoint mirrors DB ground truth
    assert exp["pending"] >= 1                                      # the source-less row really stayed pending
    assert s["locations"]["by_status"].get("active", 0) == exp["active"]
    assert s["locations"]["by_status"].get("pending", 0) == exp["pending"]
    assert s["locations"]["public"] == exp["public"]
    assert s["coverage"]["states_covered"] == exp["states"]
    # engagement sections present and zero on a fresh slate
    assert s["community"]["photos"]["total"] == 0
    assert s["community"]["reports"]["open"] == 0
    assert set(s["community"]) >= {"votes", "pin_corrections", "field_corrections",
                                   "attribute_votes", "photos", "reports", "pending_submissions"}
