"""Photo pin-correction hardening (migration 0012).

Pre-prod audit found the photo path (0004 recompute_image) moved the canonical pin with no
distance cap, no engagement tier, no audit trail, and no coordinate bounds — a parallel, uncapped
teleport around every guard the drag-pin path enforces. These tests pin the closed behaviour:
  * a photo-suggested move beyond the 2 km origin cap does NOT auto-apply (but the photo still vouches);
  * the uploader cannot self-vote their own correction photo;
  * out-of-range / non-finite suggested coordinates are rejected at upload (and by a DB CHECK);
  * an applied photo move writes a revertible moderation_audit row.
"""
import io
import uuid

import pytest
from conftest import requires_db
from PIL import Image

TOK = "dev-mock-token"
OP_TOKEN = "test-operator-secret-0123456789"


@pytest.fixture()
def op():
    from app.config import settings
    prev = settings.operator_token
    settings.operator_token = OP_TOKEN
    yield {"X-Operator-Token": OP_TOKEN}
    settings.operator_token = prev


@pytest.fixture()
def media_tmp(tmp_path):
    from app.config import settings
    prev = settings.media_dir
    settings.media_dir = str(tmp_path)
    yield tmp_path
    settings.media_dir = prev


def _mk_location(conn, name, lat=40.00, lon=-83.00, sources=("crowd",)):
    row = conn.execute(
        "INSERT INTO locations (geom, name, org_type, status, confidence) "
        "VALUES (ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, 'drop_bin', 'active'::location_status, 60) "
        "RETURNING id", (lon, lat, name),
    ).fetchone()["id"]
    for code in sources:
        conn.execute(
            "INSERT INTO location_sources (location_id, source_code, source_ref, source_geom) "
            "VALUES (%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326))",
            (row, code, f"{code}/{uuid.uuid4()}", lon, lat))
    conn.commit()
    return row


def _mk_correction_image(conn, loc_id, slat, slon, submitter="seed-ip"):
    img = conn.execute(
        "INSERT INTO location_images (location_id, path, mime, submitter_ip_hash, suggested_lat, suggested_lon) "
        "VALUES (%s,%s,'image/jpeg',%s,%s,%s) RETURNING id",
        (loc_id, f"{uuid.uuid4().hex}.jpg", submitter, slat, slon),
    ).fetchone()["id"]
    conn.commit()
    return img


def _geom(conn, loc_id):
    conn.rollback()
    r = conn.execute("SELECT round(ST_Y(geom)::numeric,2) AS lat, round(ST_X(geom)::numeric,2) AS lon "
                     "FROM locations WHERE id=%s", (loc_id,)).fetchone()
    return float(r["lat"]), float(r["lon"])


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (120, 130, 140)).save(buf, format="JPEG")
    return buf.getvalue()


@requires_db
def test_photo_move_beyond_2km_does_not_apply(conn, client):
    """A photo suggesting a point far outside the 2 km origin cap gets vouched (score reaches 3)
    but must NOT move the canonical pin — parity with the drag-pin origin anchor."""
    loc = _mk_location(conn, "far photo move", lat=40.00, lon=-83.00)
    img = _mk_correction_image(conn, loc, slat=41.00, slon=-83.00)  # ~111 km north of origin
    for i in range(3):
        r = client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                        headers={"X-Real-IP": f"10.30.0.{i}"})
        assert r.status_code == 200, r.text
    row = conn.execute("SELECT score, applied FROM location_images WHERE id=%s", (img,)).fetchone()
    assert row["score"] == 3 and row["applied"] is False   # vouched, but the move was refused
    assert _geom(conn, loc) == (40.00, -83.00)             # pin did not teleport


@requires_db
def test_photo_move_within_cap_applies_and_is_revertible(conn, client, op):
    """A legitimate small photo move (<2 km) still applies, now with a moderation_audit row so an
    operator can revert it — the same accountability the drag-pin path already had."""
    loc = _mk_location(conn, "near photo move", lat=40.00, lon=-83.00)
    img = _mk_correction_image(conn, loc, slat=40.01, slon=-83.01)  # ~1.4 km from origin
    for i in range(3):
        client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"10.31.0.{i}"})
    assert conn.execute("SELECT applied FROM location_images WHERE id=%s", (img,)).fetchone()["applied"] is True
    assert _geom(conn, loc) == (40.01, -83.01)

    audit = client.get(f"/api/admin/locations/{loc}/audit", headers=op).json()["audit"]
    assert len(audit) == 1 and audit[0]["kind"] == "pin_correction"
    rev = client.post(f"/api/admin/audit/{audit[0]['id']}/revert", json={"note": "photo move undo"}, headers=op)
    assert rev.status_code == 200 and rev.json()["result"] == "reverted"
    assert _geom(conn, loc) == (40.00, -83.00)             # restored to origin


@requires_db
def test_image_self_vote_rejected(conn, client):
    """The uploader cannot cast a helpful vote on their own correction photo (409 self_vote)."""
    from app.security import ip_hash
    loc = _mk_location(conn, "self vote photo", lat=40.00, lon=-83.00)
    img = _mk_correction_image(conn, loc, slat=40.005, slon=-83.005, submitter=ip_hash("203.0.90.1"))
    r = client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                    headers={"X-Real-IP": "203.0.90.1"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "self_vote"
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM image_votes WHERE image_id=%s", (img,)).fetchone()["n"] == 0


@requires_db
@pytest.mark.parametrize("lat,lon", [(91.0, -83.0), (40.0, 200.0), (float("nan"), -83.0)])
def test_upload_out_of_range_coords_rejected(conn, client, media_tmp, lat, lon):
    loc = _mk_location(conn, f"oob coords {lat}-{lon}", lat=40.0, lon=-83.0)
    r = client.post(f"/api/locations/{loc}/images",
                    files={"file": ("p.jpg", _jpeg(), "image/jpeg")},
                    data={"turnstile_token": TOK, "suggested_lat": str(lat), "suggested_lon": str(lon)},
                    headers={"X-Real-IP": "203.0.91.1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_coords"
    assert not list(media_tmp.glob("*.jpg"))   # rejected before any file is written
