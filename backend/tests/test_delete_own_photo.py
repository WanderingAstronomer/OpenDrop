"""Delete-my-photo — a submitter may remove their OWN still-unverified photo.

Companion to the moderation/takedown tests: those cover the operator hard-takedown path; this covers
the self-service delete a visitor gets on a photo they just added. The guard is narrow on purpose —
only the submitter (by ip-hash), only while the photo is 'pending' AND has applied no pin correction;
once it's vouched (visible), hidden, or its correction moved the pin, it's community-owned. These
tests pin: own-pending delete (row gone + file unlinked), a stranger 403s, a vouched photo 409s, and
list_images carries the `mine` flag the UI branches on.
"""
import io

import pytest
from conftest import requires_db
from PIL import Image

TOK = "dev-mock-token"


@pytest.fixture()
def media_tmp(tmp_path):
    from app.config import settings
    prev = settings.media_dir
    settings.media_dir = str(tmp_path)
    yield tmp_path
    settings.media_dir = prev


def _mk_location(conn, name="Delete-photo target"):
    row = conn.execute(
        "INSERT INTO locations (geom, name, org_type, status, confidence) "
        "VALUES (ST_SetSRID(ST_MakePoint(-83.0,40.0),4326), %s, 'drop_bin', 'active'::location_status, 60) "
        "RETURNING id", (name,),
    ).fetchone()["id"]
    conn.commit()
    return row


def _jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (60, 40), (120, 130, 140)).save(buf, format="JPEG")
    return buf.getvalue()


def _upload(client, loc, ip):
    """Upload a real photo through the API (writes an actual file under media_dir); returns its id."""
    r = client.post(f"/api/locations/{loc}/images",
                    files={"file": ("p.jpg", _jpeg(), "image/jpeg")},
                    data={"turnstile_token": TOK},
                    headers={"X-Real-IP": ip})
    assert r.status_code == 200, r.text
    return r.json()["image_id"]


@requires_db
def test_owner_deletes_own_pending_photo(conn, client, media_tmp):
    loc = _mk_location(conn)
    img = _upload(client, loc, "203.0.60.1")
    assert len(list(media_tmp.glob("*.jpg"))) == 1               # file is on disk

    d = client.request("DELETE", f"/api/images/{img}",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": "203.0.60.1"})
    assert d.status_code == 200, d.text
    assert d.json() == {"deleted": True}

    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM location_images WHERE id=%s", (img,)).fetchone()["n"] == 0
    assert not list(media_tmp.glob("*.jpg"))                     # media file unlinked too


@requires_db
def test_delete_by_non_submitter_403(conn, client, media_tmp):
    loc = _mk_location(conn, "Not your photo")
    img = _upload(client, loc, "203.0.61.1")
    d = client.request("DELETE", f"/api/images/{img}",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": "203.0.61.2"})
    assert d.status_code == 403 and d.json()["error"]["code"] == "not_owner"
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM location_images WHERE id=%s", (img,)).fetchone()["n"] == 1
    assert len(list(media_tmp.glob("*.jpg"))) == 1               # untouched


@requires_db
def test_delete_vouched_photo_409(conn, client, media_tmp):
    loc = _mk_location(conn, "Vouched photo")
    img = _upload(client, loc, "203.0.62.1")
    conn.execute("UPDATE location_images SET status='visible' WHERE id=%s", (img,))  # community-vouched
    conn.commit()

    d = client.request("DELETE", f"/api/images/{img}",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": "203.0.62.1"})
    assert d.status_code == 409 and d.json()["error"]["code"] == "not_pending"
    conn.rollback()
    assert conn.execute("SELECT count(*) AS n FROM location_images WHERE id=%s", (img,)).fetchone()["n"] == 1
    assert len(list(media_tmp.glob("*.jpg"))) == 1               # not unlinked


@requires_db
def test_delete_applied_correction_photo_409(conn, client, media_tmp):
    """Even while 'pending', a photo whose correction already moved the pin is community-owned."""
    loc = _mk_location(conn, "Applied correction photo")
    img = _upload(client, loc, "203.0.63.1")
    conn.execute("UPDATE location_images SET applied=true WHERE id=%s", (img,))
    conn.commit()
    d = client.request("DELETE", f"/api/images/{img}",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": "203.0.63.1"})
    assert d.status_code == 409 and d.json()["error"]["code"] == "not_pending"


@requires_db
def test_delete_missing_photo_404(conn, client, media_tmp):
    d = client.request("DELETE", "/api/images/99777333",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": "203.0.64.1"})
    assert d.status_code == 404 and d.json()["error"]["code"] == "not_found"


@requires_db
def test_delete_missing_token_403(conn, client, media_tmp):
    loc = _mk_location(conn, "No token delete")
    img = _upload(client, loc, "203.0.65.1")
    d = client.request("DELETE", f"/api/images/{img}",
                       json={}, headers={"X-Real-IP": "203.0.65.1"})
    assert d.status_code == 403 and d.json()["error"]["code"] == "turnstile_failed"


@requires_db
def test_list_images_mine_flag(conn, client, media_tmp):
    loc = _mk_location(conn, "Mine flag")
    _upload(client, loc, "203.0.66.1")
    # The uploader sees mine=true; a different visitor sees mine=false (pending needs include_low).
    mine = client.get(f"/api/locations/{loc}/images?include_low=true",
                      headers={"X-Real-IP": "203.0.66.1"}).json()["images"]
    assert len(mine) == 1 and mine[0]["mine"] is True
    theirs = client.get(f"/api/locations/{loc}/images?include_low=true",
                        headers={"X-Real-IP": "203.0.66.2"}).json()["images"]
    assert len(theirs) == 1 and theirs[0]["mine"] is False
