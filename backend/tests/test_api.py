"""API + trigger behavior tests (require the DB up). Cover the Phase-4 checks:
vote raises confidence, denies -> pending (single & multi-source override),
24h cooldown 429, missing-token 403 (dev mock), export excludes non-redistributable."""
import uuid

import pytest

from conftest import requires_db

TOK = "dev-mock-token"  # any non-empty token passes the CF test secret


def _mk_location(conn, name, lat=39.96, lon=-82.99, sources=("salvation_army",), org_type="charity_store"):
    """Insert a location + its source rows (triggers recompute confidence). Returns id."""
    row = conn.execute(
        "INSERT INTO locations (geom, name, org_type) "
        "VALUES (ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, %s) RETURNING id",
        (lon, lat, name, org_type),
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


def _confidence(conn, loc_id):
    return float(conn.execute("SELECT confidence FROM locations WHERE id=%s", (loc_id,)).fetchone()["confidence"])


def _status(conn, loc_id):
    return conn.execute("SELECT status FROM locations WHERE id=%s", (loc_id,)).fetchone()["status"]


@requires_db
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["db"] is True


@requires_db
def test_meta_shape(client):
    r = client.get("/api/meta")
    assert r.status_code == 200
    body = r.json()
    assert "sources" in body and "turnstile_sitekey" in body and "confidence_buckets" in body
    assert "coverage" in body  # initial-view hint (may be null until data is seeded)


@requires_db
def test_meta_coverage_brackets_active_data(client, conn):
    """coverage drives the map's opening view, so its bbox must actually contain the live data."""
    lat, lon = 39.961, -82.991
    conn.execute(
        "INSERT INTO locations (geom, name, org_type, status) "
        "VALUES (ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, 'drop_bin', 'active')",
        (lon, lat, f"cov-{uuid.uuid4()}"),
    )
    conn.commit()
    cov = client.get("/api/meta").json()["coverage"]
    assert cov is not None, "coverage must be present once an active location exists"
    s, w, n, e = cov["bbox"]
    assert s <= lat <= n and w <= lon <= e
    cy, cx = cov["center"]
    assert s <= cy <= n and w <= cx <= e


@requires_db
def test_vote_missing_token_403(client, conn):
    loc = _mk_location(conn, "SA test missing token")
    r = client.post(f"/api/locations/{loc}/vote", json={"vote": "confirm"})  # no token
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "turnstile_failed"


@requires_db
def test_vote_confirm_raises_then_cooldown(client, conn):
    loc = _mk_location(conn, "SA confirm+cooldown")
    before = _confidence(conn, loc)
    r = client.post(f"/api/locations/{loc}/vote", json={"vote": "confirm", "turnstile_token": TOK},
                    headers={"X-Real-IP": "203.0.113.10"})
    assert r.status_code == 200
    assert r.json()["confidence"] > before
    # second vote from same IP within 24h -> cooldown
    r2 = client.post(f"/api/locations/{loc}/vote", json={"vote": "confirm", "turnstile_token": TOK},
                     headers={"X-Real-IP": "203.0.113.10"})
    assert r2.status_code == 429 and r2.json()["error"]["code"] == "cooldown_active"


@requires_db
def test_forged_xff_does_not_change_ip(client, conn):
    """A forged X-Forwarded-For must NOT bypass the cooldown — only X-Real-IP (set by nginx) is trusted."""
    loc = _mk_location(conn, "SA xff")
    h = {"X-Real-IP": "203.0.113.20", "X-Forwarded-For": "1.2.3.4"}
    assert client.post(f"/api/locations/{loc}/vote", json={"vote": "confirm", "turnstile_token": TOK}, headers=h).status_code == 200
    h2 = {"X-Real-IP": "203.0.113.20", "X-Forwarded-For": "9.9.9.9"}  # different XFF, same real IP
    assert client.post(f"/api/locations/{loc}/vote", json={"vote": "confirm", "turnstile_token": TOK}, headers=h2).status_code == 429


@requires_db
def test_single_source_denies_to_pending(client, conn):
    loc = _mk_location(conn, "SA denies", sources=("salvation_army",))
    assert _status(conn, loc) == "active"
    for i in range(4):  # 4 distinct IPs => crowd -32 => 50-32=18 < 25
        client.post(f"/api/locations/{loc}/vote", json={"vote": "deny", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"198.51.100.{i}"})
    assert _status(conn, loc) == "pending"


@requires_db
def test_multisource_override_to_pending(client, conn):
    loc = _mk_location(conn, "OSM+SA override", sources=("osm", "salvation_army"))  # source_component 85
    assert _confidence(conn, loc) >= 80 and _status(conn, loc) == "active"
    for i in range(5):  # deny-dominance override (>=5 denies, >= confirms+5)
        client.post(f"/api/locations/{loc}/vote", json={"vote": "deny", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"198.51.100.{100 + i}"})
    assert _status(conn, loc) == "pending"


@requires_db
def test_export_excludes_non_redistributable(client, conn):
    """A location whose only source is enrich-only (goodwill) must never appear in /api/export."""
    loc = _mk_location(conn, "Goodwill-only enrich", sources=("goodwill",))
    # push it 'active' via crowd confirms (source_component=0, crowd up to +30)
    for i in range(6):
        client.post(f"/api/locations/{loc}/vote", json={"vote": "confirm", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"192.0.2.{i}"})
    row = conn.execute("SELECT status, is_redistributable FROM locations WHERE id=%s", (loc,)).fetchone()
    assert row["is_redistributable"] is False
    export = client.get("/api/export").json()
    ids = {f["properties"]["id"] for f in export["features"]}
    assert loc not in ids
    # but attribution is embedded in the payload (ODbL travels with the data)
    assert "attribution" in export and isinstance(export["attribution"], list)


@requires_db
def test_map_shows_active_non_redistributable_but_export_excludes_it(client, conn):
    """Contract: the interactive map (/locations) is the INCLUSIVE community view — it filters on
    status='active' AND confidence only, NOT is_redistributable. The bulk /export view is the
    REDISTRIBUTABLE SUBSET (status='active' AND is_redistributable). So a community-validated pin
    whose only source is enrich-only (goodwill -> not redistributable) is deliberately visible on
    the map yet absent from export. This pins the asymmetry so neither path silently drifts: the
    map must never start hiding such pins, and export must never start leaking them.

    Isolated in an empty Montana bbox so the map query returns only this fixture's pin."""
    lat, lon = 46.12, -110.12
    loc = _mk_location(conn, "map-superset goodwill", lat=lat, lon=lon, sources=("goodwill",))
    for i in range(6):  # crowd confirms push confidence to ~30 (>= floor 25) -> active
        client.post(f"/api/locations/{loc}/vote", json={"vote": "confirm", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"192.0.2.{200 + i}"})
    row = conn.execute("SELECT status, is_redistributable FROM locations WHERE id=%s", (loc,)).fetchone()
    assert row["status"] == "active" and row["is_redistributable"] is False

    # Map: present (the inclusive view shows it).
    m = client.get("/api/locations", params={"bbox": "-110.2,46.0,-110.0,46.2",
                                              "cluster": "off", "min_confidence": 0}).json()
    map_ids = {f["properties"]["id"] for f in m["features"]}
    assert loc in map_ids, "active community-validated pin must appear on the interactive map"

    # Export: absent (the redistributable subset excludes it).
    export_ids = {f["properties"]["id"] for f in client.get("/api/export").json()["features"]}
    assert loc not in export_ids, "non-redistributable pin must never leak into the bulk export"


@requires_db
def test_map_shows_crowd_pending_but_not_ingest_pending(client, conn):
    """The map is the INCLUSIVE community view: it shows every active pin PLUS crowd-submitted pins
    still below the 25 activation gate (badged unconfirmed=true), so a freshly-added spot appears
    immediately for neighbors to confirm. Low-confidence INGEST-only pending pins (scraped rows with
    no crowd source) stay OFF the map. This pins all three cases so the feed can't silently drift.

    Isolated in an empty Montana bbox so the query returns only this fixture's pins."""
    lat, lon = 46.30, -110.30
    # (1) Crowd-submitted pending: crowd source -> confidence 20 -> status stays 'pending'.
    crowd = _mk_location(conn, "crowd-pending", lat=lat, lon=lon, sources=("crowd",), org_type="drop_bin")
    assert _status(conn, crowd) == "pending"
    # (2) Ingest-only pending: a scraped (osm) pin forced back to 'pending' with NO crowd source.
    ingest = _mk_location(conn, "ingest-pending", lat=lat + 0.01, lon=lon, sources=("osm",))
    conn.execute("UPDATE locations SET status='pending' WHERE id=%s", (ingest,))
    conn.commit()
    # (3) Active: normal community-visible pin.
    active = _mk_location(conn, "active-pin", lat=lat + 0.02, lon=lon, sources=("salvation_army",))
    assert _status(conn, active) == "active"

    feats = client.get("/api/locations", params={"bbox": "-110.4,46.2,-110.2,46.4",
                                                  "min_confidence": 0}).json()["features"]
    props = {f["properties"]["id"]: f["properties"] for f in feats}
    assert crowd in props and props[crowd]["unconfirmed"] is True, \
        "crowd-submitted pending pin must appear, flagged unconfirmed"
    assert ingest not in props, "ingest-only pending pin must NOT appear on the map"
    assert active in props and props[active]["unconfirmed"] is False, \
        "active pin must appear, flagged unconfirmed=false"


@requires_db
def test_submit_missing_token_403(client):
    r = client.post("/api/locations", json={"name": "X", "org_type": "drop_bin", "address": {"city": "Columbus"}})
    assert r.status_code == 403


@requires_db
def test_image_votes_promote_and_apply_pin_correction(conn, client):
    """A correction photo: helpful votes promote pending->visible, and at score 3 a SMALL move
    (<=250 m, Band A of the migration 0013 two-band safeguard) auto-moves the pin to the suggested
    location (community-validated, no moderator). Larger moves are held for review — see
    test_photo_correction_guard.py."""
    loc = _mk_location(conn, "img correction test", lat=40.00, lon=-83.00)
    # ~0.001 deg lat ~= 111 m from origin -> Band A (within the 250 m auto-apply radius).
    img = conn.execute(
        "INSERT INTO location_images (location_id, path, mime, submitter_ip_hash, suggested_lat, suggested_lon) "
        "VALUES (%s,'x.jpg','image/jpeg','h',40.001,-83.000) RETURNING id", (loc,)
    ).fetchone()["id"]
    conn.commit()
    # default list excludes a 'pending' photo; gallery (include_low) shows it
    assert client.get(f"/api/locations/{loc}/images").json()["images"] == []
    assert len(client.get(f"/api/locations/{loc}/images?include_low=true").json()["images"]) == 1
    for i in range(3):
        r = client.post(f"/api/images/{img}/vote", json={"vote": "helpful", "turnstile_token": TOK},
                        headers={"X-Real-IP": f"10.20.0.{i}"})
        assert r.status_code == 200
    row = conn.execute("SELECT score, status, applied FROM location_images WHERE id=%s", (img,)).fetchone()
    assert row["score"] == 3 and row["status"] == "visible" and row["applied"] is True
    geo = conn.execute(
        "SELECT round(ST_Y(geom)::numeric,3) AS lat, round(ST_X(geom)::numeric,3) AS lon FROM locations WHERE id=%s", (loc,)
    ).fetchone()
    assert float(geo["lat"]) == 40.001 and float(geo["lon"]) == -83.000


@requires_db
def test_image_vote_missing_token_403(conn, client):
    """The image-vote endpoint auto-applies pin corrections, so it is bot-gated like the
    location vote: a vote with no Turnstile token is rejected before any row is written."""
    loc = _mk_location(conn, "img vote no token", lat=40.00, lon=-83.00)
    img = conn.execute(
        "INSERT INTO location_images (location_id, path, mime, submitter_ip_hash) "
        "VALUES (%s,'y.jpg','image/jpeg','h') RETURNING id", (loc,)
    ).fetchone()["id"]
    conn.commit()
    r = client.post(f"/api/images/{img}/vote", json={"vote": "helpful"})  # no token
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "turnstile_failed"
    # nothing was recorded
    n = conn.execute("SELECT count(*) AS n FROM image_votes WHERE image_id=%s", (img,)).fetchone()["n"]
    assert n == 0


@requires_db
@pytest.mark.owner_only  # closure detection is pipeline work (deletes source rows) — owner role
def test_source_removal_retires_location(conn):
    """Closure detection: a location that loses its last ingest source must drop to
    confidence 0 / pending — guards the LEAST(85, NULL)=85 source-component bug."""
    loc = _mk_location(conn, "removal regression", sources=("salvation_army",))
    assert _status(conn, loc) == "active" and _confidence(conn, loc) == 50.0
    conn.execute("DELETE FROM location_sources WHERE location_id = %s", (loc,))
    conn.commit()
    assert _confidence(conn, loc) == 0.0
    assert _status(conn, loc) == "pending"


# --- reconciliation circuit breaker -----------------------------------------------------
# Closure-detection coverage (exhaustive gate + single-run / cumulative breakers + the load-level
# completeness gate) now lives in backend/tests/test_closure_detection.py, which exercises the
# post-0011 three-layer design directly. It supersedes the three salvation_army-based tests that
# used to live here (those predated the exhaustive gate and the cross-run audit ledger).
