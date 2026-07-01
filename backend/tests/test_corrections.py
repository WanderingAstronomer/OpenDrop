"""Engagement-tiered pin corrections + community signals (migration 0006 + corrections router).

Covers the trust model the product spec pins down:
  * Cold (low-engagement) corrections auto-apply on good faith.
  * Warm needs a second voter — OR the submitter standing there (GPS) — to move the pin.
  * Hot needs strong weighted support.
  * GPS only ADDS weight; the server never receives coordinates.
  * Closure asymmetry: a fresh location retires on 2 denies, a busy one survives 5.
  * Drop-a-pin submissions skip geocoding and land exactly where the pin was dropped.

All DB-backed; each test owns a fresh location, so IP reuse across tests is harmless
(engagement is per-location). Reader helpers rollback first to escape any stale snapshot.
"""
import uuid

import pytest

from conftest import requires_db

TOK = "dev-mock-token"  # any non-empty token passes the CF test secret


def _mk_location(conn, name, lat=41.50, lon=-81.70, sources=("salvation_army",), org_type="drop_bin"):
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


def _geom(conn, loc_id):
    conn.rollback()  # fresh snapshot — see whatever the API just committed
    r = conn.execute("SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon FROM locations WHERE id=%s", (loc_id,)).fetchone()
    return round(float(r["lat"]), 4), round(float(r["lon"]), 4)


def _status(conn, loc_id):
    conn.rollback()
    return conn.execute("SELECT status FROM locations WHERE id=%s", (loc_id,)).fetchone()["status"]


def _corr_status(conn, corr_id):
    conn.rollback()
    return conn.execute("SELECT status FROM location_corrections WHERE id=%s", (corr_id,)).fetchone()["status"]


def _seed_engagement(client, loc_id, n, start):
    """Raise engagement to >= n DISTINCT participants via attribute ratings from distinct IPs."""
    for i in range(n):
        r = client.post(
            f"/api/locations/{loc_id}/attributes",
            json={"attribute": "safety", "value": (i % 3) + 1, "turnstile_token": TOK},
            headers={"X-Real-IP": f"100.64.{start}.{i}"},
        )
        assert r.status_code == 200, r.text


def _propose(client, loc_id, lat, lon, ip, gps=False, note=None):
    return client.post(
        f"/api/locations/{loc_id}/corrections",
        json={"suggested_lat": lat, "suggested_lon": lon, "gps_corroborated": gps,
              "note": note, "turnstile_token": TOK},
        headers={"X-Real-IP": ip},
    )


def _confirm(client, corr_id, ip, confirm=True, gps=False):
    return client.post(
        f"/api/corrections/{corr_id}/vote",
        json={"confirm": confirm, "gps_corroborated": gps, "turnstile_token": TOK},
        headers={"X-Real-IP": ip},
    )


# --- Cold: good-faith instant apply ----------------------------------------
@requires_db
def test_cold_correction_applies_on_good_faith(conn, client):
    loc = _mk_location(conn, "cold faith bin")
    r = _propose(client, loc, 41.5011, -81.70, "198.51.10.1")  # ~120 m north
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["applied"] is True and b["status"] == "applied"
    assert b["required_support"] == 1 and b["support"] >= 1
    assert _geom(conn, loc) == (41.5011, -81.70)  # pin moved


@requires_db
def test_correction_missing_token_403(conn, client):
    loc = _mk_location(conn, "corr no token")
    r = client.post(f"/api/locations/{loc}/corrections",
                    json={"suggested_lat": 41.5011, "suggested_lon": -81.70})
    assert r.status_code == 403 and r.json()["error"]["code"] == "turnstile_failed"
    conn.rollback()
    n = conn.execute("SELECT count(*) AS n FROM location_corrections WHERE location_id=%s", (loc,)).fetchone()["n"]
    assert n == 0  # nothing written


@requires_db
def test_correction_too_far_is_rejected(conn, client):
    loc = _mk_location(conn, "corr far", lat=41.50, lon=-81.70)
    r = _propose(client, loc, 41.50, -82.20, "198.51.15.1")  # ~42 km away
    assert r.status_code == 422 and r.json()["error"]["code"] == "move_too_far"
    assert _geom(conn, loc) == (41.50, -81.70)  # unchanged


@requires_db
def test_correction_cannot_walk_pin_past_origin(conn, client):
    """The 2 km cap is anchored to the IMMUTABLE origin, not the live pin, so a sequence of
    individually-legal small hops can't walk a pin across the map (migration 0007)."""
    loc = _mk_location(conn, "walk bin", lat=41.10, lon=-81.10)
    # Hop 1: ~1.5 km north of origin — Cold, applies on good faith.
    r1 = _propose(client, loc, 41.1135, -81.10, "198.51.40.1")
    assert r1.status_code == 200 and r1.json()["applied"] is True
    assert _geom(conn, loc) == (41.1135, -81.10)
    # Hop 2: another ~1.5 km north — only ~1.5 km from the CURRENT pin (a naive cap would allow it)
    # but ~3 km from the origin, so the API guard rejects it.
    r2 = _propose(client, loc, 41.1270, -81.10, "198.51.40.2")
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "move_too_far"
    assert _geom(conn, loc) == (41.1135, -81.10)  # pin did not walk further


@requires_db
def test_trigger_caps_move_from_origin(conn):
    """Defense in depth: even a direct INSERT that bypasses the API guard won't auto-apply a move
    that is within 2 km of the current pin but beyond 2 km of the immutable origin."""
    loc = _mk_location(conn, "trigger walk bin", lat=41.30, lon=-81.30)
    # Simulate a prior applied hop: move the live geom ~1.5 km north, leaving origin_geom at 41.30.
    conn.execute("UPDATE locations SET geom = ST_SetSRID(ST_MakePoint(%s,%s),4326) WHERE id=%s",
                 (-81.30, 41.3135, loc))
    conn.commit()
    # Direct correction ~1.5 km north of the CURRENT pin (~3 km from origin). Cold support is met,
    # so without the origin anchor the after-insert trigger would apply it.
    conn.execute(
        "INSERT INTO location_corrections (location_id, suggested_lat, suggested_lon, submitter_ip_hash) "
        "VALUES (%s,%s,%s,%s)", (loc, 41.3270, -81.30, "trigger-test"))
    conn.commit()
    assert _geom(conn, loc) == (41.3135, -81.30)  # geom did NOT move to 41.3270
    st = conn.execute(
        "SELECT status FROM location_corrections WHERE location_id=%s ORDER BY id DESC LIMIT 1",
        (loc,)).fetchone()["status"]
    assert st == "open"  # support met but the move is out of range -> proposal stays open


# --- Warm: needs a confirmer, or the submitter's GPS -----------------------
@requires_db
def test_warm_correction_needs_confirmation(conn, client):
    loc = _mk_location(conn, "warm bin")
    _seed_engagement(client, loc, 2, start=11)            # 2 ips; submitter makes E=3 => Warm
    r = _propose(client, loc, 41.5011, -81.70, "198.51.11.99")
    b = r.json()
    assert b["status"] == "open" and b["applied"] is False
    assert b["required_support"] == 2 and b["support"] == 1
    assert _geom(conn, loc) == (41.50, -81.70)            # not moved yet
    r2 = _confirm(client, b["correction_id"], "198.51.11.50")  # a different ip confirms
    b2 = r2.json()
    assert b2["applied"] is True and b2["status"] == "applied" and b2["support"] >= 2
    assert _geom(conn, loc) == (41.5011, -81.70)          # now moved


@requires_db
def test_warm_gps_submitter_applies_alone(conn, client):
    loc = _mk_location(conn, "warm gps bin")
    _seed_engagement(client, loc, 2, start=12)
    r = _propose(client, loc, 41.5011, -81.70, "198.51.12.99", gps=True)  # "I'm standing here"
    b = r.json()
    assert b["applied"] is True and b["support"] == 2 and b["required_support"] == 2
    assert _geom(conn, loc) == (41.5011, -81.70)


@requires_db
def test_hot_gps_confirmer_counts_double(conn, client):
    """A confirmer physically at the suggested point (gps_corroborated) carries weight 2 — the same
    on-site boost the submitter gets — so TWO GPS confirmers clear a Hot correction that would
    otherwise need THREE plain ones (cf. test_hot_tier_requires_strong_support). This guards the
    frontend confirm-vote path, which must actually send gps_corroborated=true when on-site rather
    than hard-coding false. Server weighting: v_support = (1 + submitter_gps) + SUM(1 + voter_gps)."""
    loc = _mk_location(conn, "hot gps confirmer bin")
    _seed_engagement(client, loc, 15, start=60)  # 15 distinct ips => Hot, required_support 4
    cid = _propose(client, loc, 41.5011, -81.70, "198.51.60.250").json()["correction_id"]

    b1 = _confirm(client, cid, "198.51.60.101", gps=True).json()  # +2 => support 1 + 2 = 3 (< 4)
    assert b1["applied"] is False and b1["support"] == 3  # crisp proof the GPS confirmer weighed 2

    b2 = _confirm(client, cid, "198.51.60.102", gps=True).json()  # +2 => 5 >= 4 -> applies
    assert b2["applied"] is True and b2["support"] >= 4
    assert _geom(conn, loc) == (41.5011, -81.70)  # pin moved, on only two confirmers


@requires_db
def test_submitter_cannot_confirm_own_correction(conn, client):
    loc = _mk_location(conn, "selfvote bin")
    _seed_engagement(client, loc, 2, start=13)
    r = _propose(client, loc, 41.5011, -81.70, "198.51.13.99")
    cid = r.json()["correction_id"]
    assert r.json()["status"] == "open"
    r2 = _confirm(client, cid, "198.51.13.99")  # same ip as submitter
    assert r2.status_code == 409 and r2.json()["error"]["code"] == "self_vote"


@requires_db
def test_correction_rejected_by_downvotes(conn, client):
    loc = _mk_location(conn, "reject bin")
    _seed_engagement(client, loc, 2, start=14)
    cid = _propose(client, loc, 41.5011, -81.70, "198.51.14.99").json()["correction_id"]
    _confirm(client, cid, "198.51.14.1", confirm=False)
    _confirm(client, cid, "198.51.14.2", confirm=False)
    assert _corr_status(conn, cid) == "rejected"
    assert _geom(conn, loc) == (41.50, -81.70)  # pin never moved
    detail = client.get(f"/api/locations/{loc}").json()
    assert all(c["id"] != cid for c in detail["open_corrections"])


# --- Hot: strong weighted support ------------------------------------------
@requires_db
def test_hot_tier_requires_strong_support(conn, client):
    loc = _mk_location(conn, "hot bin")
    _seed_engagement(client, loc, 15, start=20)  # 15 distinct ips => Hot
    b = _propose(client, loc, 41.5011, -81.70, "198.51.20.250").json()
    assert b["required_support"] == 4 and b["status"] == "open" and b["support"] == 1
    cid = b["correction_id"]
    last = None
    for ip in ("198.51.20.101", "198.51.20.102", "198.51.20.103"):  # +3 weight => support 4
        last = _confirm(client, cid, ip)
    bb = last.json()
    assert bb["applied"] is True and bb["support"] >= 4
    assert _geom(conn, loc) == (41.5011, -81.70)


# --- Closure asymmetry: fresh retires easily, busy resists -----------------
@requires_db
def test_retire_asymmetry_cold_vs_hot(conn, client):
    # Cold multi-source: 2 denies are enough to retire it.
    cold = _mk_location(conn, "cold retire", lat=41.10, lon=-81.10, sources=("osm", "salvation_army"))
    assert _status(conn, cold) == "active"
    for ip in ("198.51.30.1", "198.51.30.2"):
        client.post(f"/api/locations/{cold}/vote", json={"vote": "deny", "turnstile_token": TOK},
                    headers={"X-Real-IP": ip})
    assert _status(conn, cold) == "pending"

    # Hot multi-source: the SAME source strength survives 5 denies (floor is 8).
    hot = _mk_location(conn, "hot retire", lat=41.20, lon=-81.20, sources=("osm", "salvation_army"))
    _seed_engagement(client, hot, 15, start=31)
    assert _status(conn, hot) == "active"
    for i in range(5):
        client.post(f"/api/locations/{hot}/vote", json={"vote": "deny", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"198.51.32.{i}"})
    assert _status(conn, hot) == "active"


@requires_db
def test_retire_requires_strict_deny_dominance(conn, client):
    """A balanced community (equal confirms and denies) is a DISPUTE, not a removal consensus —
    the pin stays active. Denies must STRICTLY exceed confirms to retire (migration 0007)."""
    loc = _mk_location(conn, "tie dispute", lat=41.40, lon=-81.40, sources=("osm", "salvation_army"))
    assert _status(conn, loc) == "active"
    for i in range(4):  # 4 confirms
        client.post(f"/api/locations/{loc}/vote", json={"vote": "confirm", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"198.51.50.{i}"})
    for i in range(4):  # 4 denies -> meets the Warm floor (4) but only TIES the confirms
        client.post(f"/api/locations/{loc}/vote", json={"vote": "deny", "turnstile_token": TOK},
                    headers={"X-Real-IP": f"198.51.51.{i}"})
    assert _status(conn, loc) == "active"  # tie => stays active
    # One more deny makes denies strictly dominate (5 > 4) and still meets the floor -> pending.
    client.post(f"/api/locations/{loc}/vote", json={"vote": "deny", "turnstile_token": TOK},
                headers={"X-Real-IP": "198.51.51.9"})
    assert _status(conn, loc) == "pending"


# --- Community attribute ratings -------------------------------------------
@requires_db
def test_attributes_aggregate_and_validate(conn, client):
    loc = _mk_location(conn, "attr bin")
    last = None
    for i, v in enumerate((1, 2, 3)):
        last = client.post(f"/api/locations/{loc}/attributes",
                           json={"attribute": "safety", "value": v, "turnstile_token": TOK},
                           headers={"X-Real-IP": f"203.0.50.{i}"})
        assert last.status_code == 200
    agg = last.json()["attributes"]["safety"]
    assert agg["count"] == 3 and agg["avg"] == 2.0
    detail = client.get(f"/api/locations/{loc}").json()
    assert detail["attributes"]["safety"]["count"] == 3
    assert detail["tier"] == "warm"  # 3 distinct participants
    # safety is a 1..3 scale; 4 is out of range even though the column allows up to 50 (bins).
    bad = client.post(f"/api/locations/{loc}/attributes",
                      json={"attribute": "safety", "value": 4, "turnstile_token": TOK},
                      headers={"X-Real-IP": "203.0.50.9"})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_value"


@requires_db
def test_attribute_rate_limit(conn, client):
    """A per-IP-per-day cap bounds how many distinct spots one IP can rate; refining an existing
    rating is always allowed (it's an UPDATE, not a new row)."""
    from app.config import settings
    from app.security import ip_hash
    loc = _mk_location(conn, "attr ratelimit")
    ip = "203.0.77.7"
    # This cap counts per IP across ALL locations in a 24h window, so clear any rows this IP left
    # behind on a prior run — keeps the test idempotent against a persistent test DB.
    conn.execute("DELETE FROM attribute_votes WHERE ip_hash = %s", (ip_hash(ip),))
    conn.commit()
    orig = settings.attributes_per_ip_per_day
    settings.attributes_per_ip_per_day = 2
    try:
        for attr in ("safety", "condition"):  # two NEW pairs fill the cap
            r = client.post(f"/api/locations/{loc}/attributes",
                            json={"attribute": attr, "value": 2, "turnstile_token": TOK},
                            headers={"X-Real-IP": ip})
            assert r.status_code == 200, r.text
        # A third NEW pair is over the cap.
        over = client.post(f"/api/locations/{loc}/attributes",
                           json={"attribute": "bins", "value": 3, "turnstile_token": TOK},
                           headers={"X-Real-IP": ip})
        assert over.status_code == 429 and over.json()["error"]["code"] == "attribute_cooldown"
        # But refining an EXISTING rating still works even at the cap.
        again = client.post(f"/api/locations/{loc}/attributes",
                            json={"attribute": "safety", "value": 3, "turnstile_token": TOK},
                            headers={"X-Real-IP": ip})
        assert again.status_code == 200, again.text
    finally:
        settings.attributes_per_ip_per_day = orig


# --- Drop-a-pin submission --------------------------------------------------
@requires_db
@pytest.mark.owner_only  # endpoint works as app role; the DELETE self-clean below needs the owner
def test_drop_a_pin_creates_at_exact_coords(conn, client):
    # This test PROMOTES a location at a fixed coord+name, so on a reused DB its own prior output
    # sits 0 m away with an identical name and trips dupe-detection (<=300 m AND name_sim>=0.4) ->
    # the second run would see status="duplicate". Clear that self-collision first so the test is
    # idempotent against a persistent DB (same spirit as test_attribute_rate_limit's self-clean).
    # location_sources / pending_locations FKs are CASCADE / SET NULL, so this is a clean removal.
    name = "Dropped Pin Donation Box"
    conn.execute("DELETE FROM locations WHERE name = %s", (name,))
    conn.execute("DELETE FROM pending_locations WHERE name = %s", (name,))
    conn.commit()
    # Supplying a city means reverse-geocode (network) is skipped — the pin is authoritative.
    r = client.post("/api/locations", json={
        "name": name, "org_type": "drop_bin",
        "address": {"city": "Clevtest"}, "lat": 41.4993, "lon": -81.6944,
        "turnstile_token": TOK,
    }, headers={"X-Real-IP": "205.0.0.1"})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["geocoded"] is True and b["location_id"] is not None and b["status"] == "promoted"
    conn.rollback()
    g = conn.execute("SELECT ST_Y(geom) AS lat, ST_X(geom) AS lon FROM locations WHERE id=%s",
                     (b["location_id"],)).fetchone()
    assert round(float(g["lat"]), 4) == 41.4993 and round(float(g["lon"]), 4) == -81.6944
