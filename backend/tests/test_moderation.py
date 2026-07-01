"""Production-hardening surface (migration 0010 + routers/moderation.py):

  * Seed-source GATE — renaming/re-addressing an AUTHORITATIVELY-sourced location no longer
    auto-applies on a lone submitter (needs >=1 confirmer); org_type and crowd-only pins are exempt.
  * Public reporting — report a location (complaint only, never auto-hidden) or a photo (soft-hidden
    once enough distinct reporters flag it). Turnstile + reason screening + rate-limit guards.
  * Operator surface — every /admin route 404s without a valid X-Operator-Token (invisible to
    probes), and works with one. Takedown/restore for locations + photos.
  * Audit / revert — auto-applied corrections land in moderation_audit and an operator can revert
    one, or bulk-revert everything a single actor pushed, restoring the prior column values.

All DB-backed; each test owns fresh rows. The `op` fixture flips the in-process OPERATOR_TOKEN on
the shared settings singleton (require_operator reads it live) and restores it on teardown.
"""
import uuid

import pytest
from conftest import requires_db

TOK = "dev-mock-token"          # any non-empty token passes the CF test secret
OP_TOKEN = "test-operator-secret-0123456789"


@pytest.fixture()
def op():
    from app.config import settings
    prev = settings.operator_token
    settings.operator_token = OP_TOKEN
    yield {"X-Operator-Token": OP_TOKEN}
    settings.operator_token = prev


def _mk_location(conn, name, lat=40.10, lon=-83.10, sources=("salvation_army",),
                 org_type="drop_bin", status="active", confidence=60):
    row = conn.execute(
        "INSERT INTO locations (geom, name, org_type, status, confidence) "
        "VALUES (ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, %s, %s::location_status, %s) RETURNING id",
        (lon, lat, name, org_type, status, confidence),
    ).fetchone()
    loc_id = row["id"]
    for code in sources:
        conn.execute(
            "INSERT INTO location_sources (location_id, source_code, source_ref, source_geom) "
            "VALUES (%s, %s, %s, ST_SetSRID(ST_MakePoint(%s,%s),4326))",
            (loc_id, code, f"{code}/{uuid.uuid4()}", lon, lat),
        )
    conn.commit()
    return loc_id


def _mk_image(conn, loc_id, path=None, status="visible"):
    path = path or f"{uuid.uuid4().hex}.jpg"
    row = conn.execute(
        "INSERT INTO location_images (location_id, path, mime, submitter_ip_hash, status) "
        "VALUES (%s, %s, 'image/jpeg', 'iphash', %s::image_status) RETURNING id",
        (loc_id, path, status),
    ).fetchone()
    conn.commit()
    return row["id"], path


def _name(conn, loc_id):
    conn.rollback()
    return conn.execute("SELECT name FROM locations WHERE id=%s", (loc_id,)).fetchone()["name"]


# ----------------------------------------------------------------- seed-source gate

@requires_db
def test_authoritative_field_gate_blocks_lone_rename(conn, client):
    """A lone good-faith rename on an authoritatively-sourced (salvation_army) Cold location must
    NOT auto-apply — the gate bumps required_support to 2. One confirmer then lands it."""
    loc = _mk_location(conn, "Authoritative Charity Bin")
    r = client.post(f"/api/locations/{loc}/field-corrections",
                    json={"field": "name", "value": "Renamed By One Person", "turnstile_token": TOK},
                    headers={"X-Real-IP": "198.60.1.1"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["status"] == "open" and b["applied"] is False
    assert b["required_support"] == 2          # gated up from the Cold default of 1
    assert _name(conn, loc) == "Authoritative Charity Bin"

    r2 = client.post(f"/api/field-corrections/{b['correction_id']}/vote",
                     json={"confirm": True, "turnstile_token": TOK}, headers={"X-Real-IP": "198.60.1.2"})
    assert r2.status_code == 200 and r2.json()["applied"] is True
    assert _name(conn, loc) == "Renamed By One Person"


@requires_db
def test_authoritative_org_type_change_is_not_gated(conn, client):
    """org_type is not identity-critical — it keeps the normal Cold auto-apply even on a seed row."""
    loc = _mk_location(conn, "Type change ok", org_type="drop_bin")
    r = client.post(f"/api/locations/{loc}/field-corrections",
                    json={"field": "org_type", "value": "thrift_store", "turnstile_token": TOK},
                    headers={"X-Real-IP": "198.60.2.1"})
    assert r.status_code == 200 and r.json()["applied"] is True
    conn.rollback()
    assert conn.execute("SELECT org_type FROM locations WHERE id=%s", (loc,)).fetchone()["org_type"] == "thrift_store"


# ----------------------------------------------------------------- public: report a location

@requires_db
def test_report_location_files_complaint_without_hiding(conn, client):
    loc = _mk_location(conn, "Reportable Bin")
    r = client.post(f"/api/locations/{loc}/report",
                    json={"reason": "looks fake", "turnstile_token": TOK}, headers={"X-Real-IP": "198.61.1.1"})
    assert r.status_code == 200, r.text
    assert r.json()["hidden"] is False
    assert client.get(f"/api/locations/{loc}").status_code == 200       # still public
    conn.rollback()
    n = conn.execute(
        "SELECT count(*) AS n FROM content_reports "
        "WHERE target_type='location' AND target_id=%s AND resolved_at IS NULL", (loc,)).fetchone()["n"]
    assert n == 1


@requires_db
def test_report_missing_location_404(client):
    r = client.post("/api/locations/99888777/report",
                    json={"turnstile_token": TOK}, headers={"X-Real-IP": "198.61.2.1"})
    assert r.status_code == 404


@requires_db
def test_report_reason_denylist_rejected(conn, client):
    loc = _mk_location(conn, "Report screen bin")
    r = client.post(f"/api/locations/{loc}/report",
                    json={"reason": "free money click here", "turnstile_token": TOK},
                    headers={"X-Real-IP": "198.61.3.1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "rejected"


@requires_db
def test_report_missing_token_403(conn, client):
    loc = _mk_location(conn, "Report token bin")
    r = client.post(f"/api/locations/{loc}/report", json={"reason": "x"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "turnstile_failed"


# ----------------------------------------------------------------- public: report a photo

@requires_db
def test_image_report_threshold_soft_hides_photo(conn, client):
    loc = _mk_location(conn, "Photo bin")
    img, _ = _mk_image(conn, loc, status="visible")
    r1 = client.post(f"/api/images/{img}/report",
                     json={"reason": "blurry", "turnstile_token": TOK}, headers={"X-Real-IP": "198.62.1.1"})
    assert r1.status_code == 200 and r1.json()["hidden"] is False        # lone report: complaint only

    r2 = client.post(f"/api/images/{img}/report",
                     json={"turnstile_token": TOK}, headers={"X-Real-IP": "198.62.1.2"})
    assert r2.status_code == 200 and r2.json()["hidden"] is True         # 2nd distinct reporter -> hidden

    conn.rollback()
    assert conn.execute("SELECT removed_at FROM location_images WHERE id=%s", (img,)).fetchone()["removed_at"] is not None
    imgs = client.get(f"/api/locations/{loc}/images?include_low=true").json()["images"]
    assert all(i["id"] != img for i in imgs)                              # dropped from the gallery


@requires_db
def test_image_report_same_ip_twice_does_not_hide(conn, client):
    loc = _mk_location(conn, "Photo single reporter bin")
    img, _ = _mk_image(conn, loc, status="visible")
    for _ in range(2):
        r = client.post(f"/api/images/{img}/report",
                        json={"turnstile_token": TOK}, headers={"X-Real-IP": "198.62.2.1"})
        assert r.status_code == 200 and r.json()["hidden"] is False      # one actor can't reach the threshold
    conn.rollback()
    assert conn.execute("SELECT removed_at FROM location_images WHERE id=%s", (img,)).fetchone()["removed_at"] is None


# ----------------------------------------------------------------- operator: gating

@requires_db
def test_admin_routes_404_without_token(client):
    assert client.get("/api/admin/reports").status_code == 404
    assert client.post("/api/admin/locations/1/takedown", json={"reason": "x"}).status_code == 404


@requires_db
def test_admin_reports_listing_with_token(client, op):
    r = client.get("/api/admin/reports", headers=op)
    assert r.status_code == 200 and "reports" in r.json()


@requires_db
def test_admin_wrong_token_404(client, op):
    r = client.get("/api/admin/reports", headers={"X-Operator-Token": "not-the-token"})
    assert r.status_code == 404


# ----------------------------------------------------------------- operator: takedown / restore

@requires_db
def test_operator_takedown_and_restore_location(conn, client, op):
    loc = _mk_location(conn, "Takedown Bin", status="active", confidence=60)
    r = client.post(f"/api/admin/locations/{loc}/takedown", json={"reason": "spam"}, headers=op)
    assert r.status_code == 200 and r.json()["status"] == "hidden"
    assert client.get(f"/api/locations/{loc}").status_code == 404       # hidden => 404 to the public

    r2 = client.post(f"/api/admin/locations/{loc}/restore", headers=op)
    assert r2.status_code == 200 and r2.json()["status"] == "active"     # confidence 60 >= 25
    assert client.get(f"/api/locations/{loc}").status_code == 200


@requires_db
def test_operator_takedown_image_unlinks_file(conn, client, op, tmp_path):
    from app.config import settings
    prev = settings.media_dir
    settings.media_dir = str(tmp_path)
    try:
        loc = _mk_location(conn, "Img takedown bin")
        fname = f"{uuid.uuid4().hex}.jpg"
        (tmp_path / fname).write_bytes(b"not-a-real-jpeg")
        img, _ = _mk_image(conn, loc, path=fname, status="visible")

        r = client.post(f"/api/admin/images/{img}/takedown", json={"reason": "abuse"}, headers=op)
        assert r.status_code == 200 and r.json()["file_removed"] is True
        assert not (tmp_path / fname).exists()                           # file unlinked from media
        conn.rollback()
        assert conn.execute("SELECT removed_at FROM location_images WHERE id=%s", (img,)).fetchone()["removed_at"] is not None

        r2 = client.post(f"/api/admin/images/{img}/restore", headers=op)
        assert r2.status_code == 200 and r2.json()["file_present"] is False   # row back, but the file is gone
    finally:
        settings.media_dir = prev


# ----------------------------------------------------------------- operator: audit + revert

@requires_db
def test_pin_correction_audit_and_revert(conn, client, op):
    loc = _mk_location(conn, "Revert pin bin", lat=40.20, lon=-83.20, sources=("crowd",))
    r = client.post(f"/api/locations/{loc}/corrections",
                    json={"suggested_lat": 40.2010, "suggested_lon": -83.20, "turnstile_token": TOK},
                    headers={"X-Real-IP": "198.63.1.1"})
    assert r.status_code == 200 and r.json()["applied"] is True
    conn.rollback()
    moved = conn.execute("SELECT round(ST_Y(geom)::numeric,4) AS lat FROM locations WHERE id=%s", (loc,)).fetchone()
    assert float(moved["lat"]) == 40.2010

    audit = client.get(f"/api/admin/locations/{loc}/audit", headers=op).json()["audit"]
    assert len(audit) == 1 and audit[0]["kind"] == "pin_correction"
    aid = audit[0]["id"]

    rev = client.post(f"/api/admin/audit/{aid}/revert", json={"note": "bad move"}, headers=op)
    assert rev.status_code == 200 and rev.json()["result"] == "reverted"
    conn.rollback()
    back = conn.execute("SELECT round(ST_Y(geom)::numeric,4) AS lat FROM locations WHERE id=%s", (loc,)).fetchone()
    assert float(back["lat"]) == 40.2000                                 # restored to origin

    # reverting the same row again is a 409 (idempotent guard)
    assert client.post(f"/api/admin/audit/{aid}/revert", json={}, headers=op).status_code == 409


@requires_db
def test_revert_actor_bulk_undoes_every_apply(conn, client, op):
    from app.security import ip_hash
    a = _mk_location(conn, "Actor bin A", lat=40.30, lon=-83.30, sources=("crowd",))
    b = _mk_location(conn, "Actor bin B", lat=40.31, lon=-83.31, sources=("crowd",))
    ip = "198.64.1.1"
    for loc in (a, b):
        rr = client.post(f"/api/locations/{loc}/field-corrections",
                         json={"field": "name", "value": f"Renamed {loc}", "turnstile_token": TOK},
                         headers={"X-Real-IP": ip})
        assert rr.status_code == 200 and rr.json()["applied"] is True

    res = client.post("/api/admin/revert-actor",
                      json={"actor_ip_hash": ip_hash(ip), "note": "bad actor"}, headers=op)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reverted"] == 2 and body["locations_affected"] == 2

    conn.rollback()
    names = conn.execute("SELECT name FROM locations WHERE id = ANY(%s)", ([a, b],)).fetchall()
    assert {n["name"] for n in names} == {"Actor bin A", "Actor bin B"}


@requires_db
def test_revert_all_unwinds_location(conn, client, op):
    """Two sequential renames by different actors, then revert-all walks back to the original."""
    loc = _mk_location(conn, "Original Name", sources=("crowd",))
    c1 = client.post(f"/api/locations/{loc}/field-corrections",
                     json={"field": "name", "value": "First Rename", "turnstile_token": TOK},
                     headers={"X-Real-IP": "198.65.1.1"})
    assert c1.json()["applied"] is True
    c2 = client.post(f"/api/locations/{loc}/field-corrections",
                     json={"field": "name", "value": "Second Rename", "turnstile_token": TOK},
                     headers={"X-Real-IP": "198.65.1.2"})
    assert c2.json()["applied"] is True
    assert _name(conn, loc) == "Second Rename"

    res = client.post(f"/api/admin/locations/{loc}/revert-all", json={"note": "undo all"}, headers=op)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reverted"] >= 1
    assert _name(conn, loc) == "Original Name"
