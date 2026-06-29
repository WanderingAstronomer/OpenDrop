"""End-to-end HTTP photo-upload tests — exercise the real multipart path through the API,
including the privacy-critical EXIF strip and the abuse/limit guards (413 / 422 / 403 / 507).

These complement the DB-level moderation tests: they prove the upload endpoint itself behaves,
not just the rows it writes. media_dir is redirected to a per-test tmp dir so nothing touches the
real volume and the EXIF assertion reads back the actual stored file.
"""
import io

import pytest
from conftest import requires_db
from PIL import Image

TOK = "dev-mock-token"
MODEL_TAG = 0x0110  # EXIF "Model" — a stand-in for all the metadata a phone embeds


def _jpeg_with_exif() -> bytes:
    """A small JPEG carrying an EXIF Model tag, so we can prove the server strips it."""
    im = Image.new("RGB", (80, 60), (90, 160, 110))
    exif = im.getexif()
    exif[MODEL_TAG] = "OpenDropTestCamera"
    buf = io.BytesIO()
    im.save(buf, format="JPEG", exif=exif.tobytes())
    return buf.getvalue()


def _mk_location(conn, name="Upload target"):
    row = conn.execute(
        "INSERT INTO locations (geom, name, org_type, status, confidence) "
        "VALUES (ST_SetSRID(ST_MakePoint(-83.0,40.0),4326), %s, 'drop_bin', 'active'::location_status, 60) "
        "RETURNING id", (name,),
    ).fetchone()
    conn.commit()
    return row["id"]


@pytest.fixture()
def media_tmp(tmp_path):
    from app.config import settings
    prev = settings.media_dir
    settings.media_dir = str(tmp_path)
    yield tmp_path
    settings.media_dir = prev


@requires_db
def test_upload_succeeds_and_strips_exif(conn, client, media_tmp):
    raw = _jpeg_with_exif()
    assert MODEL_TAG in Image.open(io.BytesIO(raw)).getexif()      # sanity: the source HAS exif

    loc = _mk_location(conn)
    r = client.post(f"/api/locations/{loc}/images",
                    files={"file": ("phone.jpg", raw, "image/jpeg")},
                    data={"turnstile_token": TOK},
                    headers={"X-Real-IP": "203.0.50.1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "pending" and body["url"].startswith("/media/")

    saved = list(media_tmp.glob("*.jpg"))
    assert len(saved) == 1                                          # re-encoded to a fresh file
    stored_exif = Image.open(saved[0]).getexif()
    assert MODEL_TAG not in stored_exif                            # …with the camera metadata gone
    assert len(stored_exif) == 0


@requires_db
def test_upload_oversize_413(conn, client, media_tmp, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "image_max_bytes", 10)           # anything real exceeds this
    loc = _mk_location(conn, "Oversize target")
    r = client.post(f"/api/locations/{loc}/images",
                    files={"file": ("big.jpg", _jpeg_with_exif(), "image/jpeg")},
                    data={"turnstile_token": TOK},
                    headers={"X-Real-IP": "203.0.50.2"})
    assert r.status_code == 413 and r.json()["error"]["code"] == "too_large"


@requires_db
def test_upload_bad_image_422(conn, client, media_tmp):
    loc = _mk_location(conn, "Bad image target")
    r = client.post(f"/api/locations/{loc}/images",
                    files={"file": ("fake.jpg", b"this is not a JPEG", "image/jpeg")},
                    data={"turnstile_token": TOK},
                    headers={"X-Real-IP": "203.0.50.3"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_image"


@requires_db
def test_upload_missing_turnstile_403(conn, client, media_tmp):
    loc = _mk_location(conn, "No token target")
    r = client.post(f"/api/locations/{loc}/images",
                    files={"file": ("x.jpg", _jpeg_with_exif(), "image/jpeg")},
                    headers={"X-Real-IP": "203.0.50.4"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "turnstile_failed"


@requires_db
def test_upload_storage_full_507(conn, client, media_tmp, monkeypatch):
    from app.config import settings
    # Simulate the media volume being at its ceiling: the cheap pre-check should refuse the upload.
    monkeypatch.setattr("app.routers.images.media_total_bytes",
                        lambda: settings.media_max_total_bytes)
    loc = _mk_location(conn, "Full storage target")
    r = client.post(f"/api/locations/{loc}/images",
                    files={"file": ("x.jpg", _jpeg_with_exif(), "image/jpeg")},
                    data={"turnstile_token": TOK},
                    headers={"X-Real-IP": "203.0.50.5"})
    assert r.status_code == 507 and r.json()["error"]["code"] == "storage_full"
    assert not list(media_tmp.glob("*.jpg"))                       # nothing written when full


@requires_db
def test_upload_missing_location_404(conn, client, media_tmp):
    r = client.post("/api/locations/99777555/images",
                    files={"file": ("x.jpg", _jpeg_with_exif(), "image/jpeg")},
                    data={"turnstile_token": TOK},
                    headers={"X-Real-IP": "203.0.50.6"})
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
