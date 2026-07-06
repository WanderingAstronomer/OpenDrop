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
    """A large-but-bounded photo move (~1.4 km, Band B) is now HELD for operator review, not
    auto-applied (owner sign-off 2026-07-05). With >=4 independent upvoters it lands in
    apply_state='pending_review' with the pin UNCHANGED; an operator then applies it, which moves
    the pin, writes the same revertible moderation_audit row, and revert still restores the origin."""
    loc = _mk_location(conn, "near photo move", lat=40.00, lon=-83.00)
    img = _mk_correction_image(conn, loc, slat=40.01, slon=-83.01)  # ~1.4 km from origin -> Band B
    for i in range(4):   # 4 DISTINCT INDEPENDENT helpful upvoters -> qualifies for the hold
        client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"10.31.0.{i}"})
    row = conn.execute("SELECT applied, apply_state FROM location_images WHERE id=%s", (img,)).fetchone()
    assert row["applied"] is False and row["apply_state"] == "pending_review"
    assert _geom(conn, loc) == (40.00, -83.00)             # pin NOT moved while pending

    # It surfaces in the operator pending-moves queue.
    q = client.get("/api/admin/images/pending-moves", headers=op).json()["pending_moves"]
    assert any(m["image_id"] == img and m["independent_voters"] >= 4 for m in q)

    # Operator applies the held move: pin moves + audit row written.
    ap = client.post(f"/api/admin/images/{img}/apply-move", headers=op)
    assert ap.status_code == 200 and ap.json()["result"] == "applied"
    assert conn.execute("SELECT applied, apply_state FROM location_images WHERE id=%s",
                        (img,)).fetchone()["apply_state"] == "approved"
    assert _geom(conn, loc) == (40.01, -83.01)

    audit = client.get(f"/api/admin/locations/{loc}/audit", headers=op).json()["audit"]
    assert len(audit) == 1 and audit[0]["kind"] == "pin_correction"
    rev = client.post(f"/api/admin/audit/{audit[0]['id']}/revert", json={"note": "photo move undo"}, headers=op)
    assert rev.status_code == 200 and rev.json()["result"] == "reverted"
    assert _geom(conn, loc) == (40.00, -83.00)             # restored to origin


@requires_db
def test_pending_moves_queue_exposes_render_fields(conn, client, op):
    """The operator queue returns everything the admin UI needs to render a review card in one
    round-trip: the evidence photo_url, the location_name, and the origin/current/suggested
    coordinates the before/after map draws — alongside distance_m and the independent-voter count."""
    loc = _mk_location(conn, "queue payload", lat=40.00, lon=-83.00)
    img = _mk_correction_image(conn, loc, slat=40.01, slon=-83.01)  # ~1.4 km from origin -> Band B
    for i in range(4):
        client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"10.36.0.{i}"})
    q = client.get("/api/admin/images/pending-moves", headers=op).json()["pending_moves"]
    row = next(m for m in q if m["image_id"] == img)
    assert row["location_id"] == loc
    assert row["location_name"] == "queue payload"
    assert row["photo_url"].startswith("/media/") and row["photo_url"].endswith(".jpg")
    assert row["photo_removed"] is False
    assert row["independent_voters"] >= 4
    assert 1300 < row["distance_m"] < 1500                                  # ~1.4 km move
    assert round(row["origin_lat"], 2) == 40.00 and round(row["origin_lon"], 2) == -83.00
    assert round(row["current_lat"], 2) == 40.00 and round(row["current_lon"], 2) == -83.00  # not moved
    assert round(row["suggested_lat"], 2) == 40.01 and round(row["suggested_lon"], 2) == -83.01


@requires_db
def test_photo_small_move_auto_applies_band_a(conn, client, op):
    """BAND A: a small move (<=250 m from origin) that clears the score gate auto-applies
    immediately (as 0012), sets apply_state='approved', and writes a revertible audit row."""
    loc = _mk_location(conn, "band a small move", lat=40.00, lon=-83.00)
    # ~0.001 deg lat ~= 111 m; well within the 250 m Band A radius.
    img = _mk_correction_image(conn, loc, slat=40.001, slon=-83.000)
    for i in range(3):
        client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"10.32.0.{i}"})
    row = conn.execute("SELECT applied, apply_state FROM location_images WHERE id=%s", (img,)).fetchone()
    assert row["applied"] is True and row["apply_state"] == "approved"
    assert _geom(conn, loc) == (40.00, -83.00)   # rounds to 2dp; check precise below
    r = conn.execute("SELECT round(ST_Y(geom)::numeric,3) AS lat, round(ST_X(geom)::numeric,3) AS lon "
                     "FROM locations WHERE id=%s", (loc,)).fetchone()
    assert (float(r["lat"]), float(r["lon"])) == (40.001, -83.000)
    audit = client.get(f"/api/admin/locations/{loc}/audit", headers=op).json()["audit"]
    assert len(audit) == 1 and audit[0]["kind"] == "pin_correction"


@requires_db
def test_photo_large_move_with_enough_voters_holds_then_operator_applies(conn, client, op):
    """BAND B: a >250 m, <=2 km move with 4 distinct independent upvoters -> pending_review, pin
    unchanged; the operator apply-move endpoint then commits it."""
    loc = _mk_location(conn, "band b enough voters", lat=40.00, lon=-83.00)
    img = _mk_correction_image(conn, loc, slat=40.01, slon=-83.00)  # ~1.1 km from origin
    for i in range(4):
        client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"10.33.0.{i}"})
    row = conn.execute("SELECT applied, apply_state FROM location_images WHERE id=%s", (img,)).fetchone()
    assert row["applied"] is False and row["apply_state"] == "pending_review"
    assert _geom(conn, loc) == (40.00, -83.00)   # not moved while pending

    ap = client.post(f"/api/admin/images/{img}/apply-move", headers=op)
    assert ap.status_code == 200 and ap.json()["result"] == "applied"
    assert _geom(conn, loc) == (40.01, -83.00)


@requires_db
def test_photo_large_move_too_few_voters_stays_none(conn, client, op):
    """BAND B floor: a >250 m, <=2 km move with only 3 independent upvoters stays apply_state='none'
    — the photo still vouches (score 3) but the pin is neither moved nor queued."""
    loc = _mk_location(conn, "band b too few voters", lat=40.00, lon=-83.00)
    img = _mk_correction_image(conn, loc, slat=40.01, slon=-83.00)  # ~1.1 km from origin
    for i in range(3):   # only 3 independent upvoters -> below the 4-voter floor
        client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"10.34.0.{i}"})
    row = conn.execute("SELECT score, applied, apply_state FROM location_images WHERE id=%s", (img,)).fetchone()
    assert row["score"] == 3 and row["applied"] is False and row["apply_state"] == "none"
    assert _geom(conn, loc) == (40.00, -83.00)   # untouched
    # Not in the pending queue either.
    q = client.get("/api/admin/images/pending-moves", headers=op).json()["pending_moves"]
    assert all(m["image_id"] != img for m in q)


@requires_db
def test_photo_move_beyond_2km_neither_applies_nor_queues(conn, client, op):
    """A >2 km move never applies AND never queues, even with plenty of independent upvoters."""
    loc = _mk_location(conn, "beyond 2km no queue", lat=40.00, lon=-83.00)
    img = _mk_correction_image(conn, loc, slat=41.00, slon=-83.00)  # ~111 km from origin
    for i in range(4):
        client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"10.35.0.{i}"})
    row = conn.execute("SELECT applied, apply_state FROM location_images WHERE id=%s", (img,)).fetchone()
    assert row["applied"] is False and row["apply_state"] == "none"
    assert _geom(conn, loc) == (40.00, -83.00)
    q = client.get("/api/admin/images/pending-moves", headers=op).json()["pending_moves"]
    assert all(m["image_id"] != img for m in q)


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
