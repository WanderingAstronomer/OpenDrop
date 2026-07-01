"""Crowd field corrections, rating deselect, and pending-resurrect on resubmit.

Covers the three behaviours added for the "out and about" issue batch:
  * #4 field corrections — propose a better name / type / org / address; same engagement-tiered
    consensus as a pin move (Cold auto-applies, Warm needs a confirmer, denies can reject), plus
    the guards (self-vote, duplicate proposal, no-op, bad value) and the GET surfacing.
  * #5 rating deselect — DELETE retracts only the caller's own attribute rating.
  * #3 pending-resurrect — re-adding a location that exists but is gated off the map by low
    confidence resurfaces it (an implicit confirm) instead of dead-ending as a duplicate.

All DB-backed; each test owns fresh rows. Reader helpers rollback first to escape any stale
snapshot, matching test_corrections.py.
"""
import uuid

import pytest

from conftest import requires_db

TOK = "dev-mock-token"  # any non-empty token passes the CF test secret


def _mk_location(conn, name, lat=40.50, lon=-82.50, sources=("salvation_army",), org_type="drop_bin"):
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


def _row(conn, loc_id):
    conn.rollback()  # fresh snapshot — see whatever the API just committed
    return conn.execute(
        "SELECT name, org_type, org_name, address_line, city, state, postal_code, status "
        "FROM locations WHERE id=%s", (loc_id,)).fetchone()


def _fc_status(conn, corr_id):
    conn.rollback()
    return conn.execute("SELECT status, applied FROM field_corrections WHERE id=%s", (corr_id,)).fetchone()


def _seed_engagement(client, loc_id, n, start):
    """Raise engagement to >= n DISTINCT participants via attribute ratings from distinct IPs."""
    for i in range(n):
        r = client.post(
            f"/api/locations/{loc_id}/attributes",
            json={"attribute": "safety", "value": (i % 3) + 1, "turnstile_token": TOK},
            headers={"X-Real-IP": f"100.65.{start}.{i}"},
        )
        assert r.status_code == 200, r.text


def _propose_field(client, loc_id, payload, ip):
    return client.post(f"/api/locations/{loc_id}/field-corrections",
                       json={**payload, "turnstile_token": TOK}, headers={"X-Real-IP": ip})


def _vote_field(client, corr_id, ip, confirm=True):
    return client.post(f"/api/field-corrections/{corr_id}/vote",
                       json={"confirm": confirm, "turnstile_token": TOK}, headers={"X-Real-IP": ip})


# --- #4 Cold: good-faith instant apply, per field --------------------------
# These exercise the UN-gated Cold path, so they use crowd-only pins. Identity-critical fields
# (name/org_name/address) on an AUTHORITATIVELY-sourced pin take the seed-source gate instead
# (>=1 confirmer) — covered separately in test_moderation.py.
@requires_db
def test_cold_field_correction_renames_on_good_faith(conn, client):
    loc = _mk_location(conn, "Bin with a typoo", sources=("crowd",))
    r = _propose_field(client, loc, {"field": "name", "value": "Corner Donation Bin"}, "198.52.10.1")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["applied"] is True and b["status"] == "applied"
    assert b["required_support"] == 1 and b["support"] >= 1
    assert _row(conn, loc)["name"] == "Corner Donation Bin"


@requires_db
def test_cold_field_correction_changes_org_type(conn, client):
    loc = _mk_location(conn, "Type fix spot", org_type="drop_bin")
    r = _propose_field(client, loc, {"field": "org_type", "value": "thrift_store"}, "198.52.11.1")
    assert r.status_code == 200 and r.json()["applied"] is True
    assert _row(conn, loc)["org_type"] == "thrift_store"


@requires_db
def test_cold_field_correction_sets_org_name(conn, client):
    loc = _mk_location(conn, "Org fix spot", sources=("crowd",))
    r = _propose_field(client, loc, {"field": "org_name", "value": "Volunteers of Testing"}, "198.52.12.1")
    assert r.status_code == 200 and r.json()["applied"] is True
    assert _row(conn, loc)["org_name"] == "Volunteers of Testing"


@requires_db
def test_cold_field_correction_updates_address(conn, client):
    loc = _mk_location(conn, "Addr fix spot", sources=("crowd",))
    r = _propose_field(client, loc, {"field": "address", "address": {
        "line": "742 Evergreen Ter", "city": "Springfield", "state": "oh", "postal_code": "44444"}},
        "198.52.13.1")
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    row = _row(conn, loc)
    assert row["address_line"] == "742 Evergreen Ter"
    assert row["city"] == "Springfield" and row["state"] == "OH" and row["postal_code"] == "44444"


# --- #4 Warm: needs a confirmer --------------------------------------------
@requires_db
def test_warm_field_correction_needs_confirmation(conn, client):
    loc = _mk_location(conn, "Warm rename bin")
    _seed_engagement(client, loc, 2, start=20)            # 2 ips; submitter makes E=3 => Warm
    r = _propose_field(client, loc, {"field": "name", "value": "Maple Street Donation Bin"}, "198.52.20.99")
    b = r.json()
    assert b["status"] == "open" and b["applied"] is False
    assert b["required_support"] == 2 and b["support"] == 1
    assert _row(conn, loc)["name"] == "Warm rename bin"   # not applied yet
    r2 = _vote_field(client, b["correction_id"], "198.52.20.50")  # a different ip confirms
    b2 = r2.json()
    assert b2["applied"] is True and b2["status"] == "applied" and b2["support"] >= 2
    assert _row(conn, loc)["name"] == "Maple Street Donation Bin"


@requires_db
def test_submitter_cannot_vote_own_field_correction(conn, client):
    loc = _mk_location(conn, "Self vote field bin")
    _seed_engagement(client, loc, 2, start=21)
    cid = _propose_field(client, loc, {"field": "name", "value": "Renamed Self"}, "198.52.21.99").json()["correction_id"]
    r = _vote_field(client, cid, "198.52.21.99")  # same ip as submitter
    assert r.status_code == 409 and r.json()["error"]["code"] == "self_vote"


@requires_db
def test_field_correction_rejected_by_downvotes(conn, client):
    loc = _mk_location(conn, "Reject field bin")
    _seed_engagement(client, loc, 2, start=22)
    cid = _propose_field(client, loc, {"field": "name", "value": "Should Not Stick"}, "198.52.22.99").json()["correction_id"]
    _vote_field(client, cid, "198.52.22.1", confirm=False)
    _vote_field(client, cid, "198.52.22.2", confirm=False)
    st = _fc_status(conn, cid)
    assert st["status"] == "rejected" and st["applied"] is False
    assert _row(conn, loc)["name"] == "Reject field bin"  # never changed
    detail = client.get(f"/api/locations/{loc}").json()
    assert all(c["id"] != cid for c in detail["open_field_corrections"])


# --- #4 Guards -------------------------------------------------------------
@requires_db
def test_field_correction_no_change_rejected(conn, client):
    loc = _mk_location(conn, "Already Named Right")
    r = _propose_field(client, loc, {"field": "name", "value": "Already Named Right"}, "198.52.30.1")
    assert r.status_code == 422 and r.json()["error"]["code"] == "no_change"


@requires_db
def test_field_correction_bad_org_type_rejected(conn, client):
    loc = _mk_location(conn, "Bad type bin")
    r = _propose_field(client, loc, {"field": "org_type", "value": "not_a_real_type"}, "198.52.31.1")
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_value"


@requires_db
def test_field_correction_duplicate_proposal_rejected(conn, client):
    loc = _mk_location(conn, "Dup proposal bin")
    _seed_engagement(client, loc, 2, start=32)  # Warm, so the first proposal stays open
    ip = "198.52.32.99"
    first = _propose_field(client, loc, {"field": "name", "value": "First Rename"}, ip)
    assert first.json()["status"] == "open"
    second = _propose_field(client, loc, {"field": "name", "value": "Second Rename"}, ip)
    assert second.status_code == 409 and second.json()["error"]["code"] == "duplicate_proposal"


@requires_db
def test_field_correction_missing_token_403(conn, client):
    loc = _mk_location(conn, "Field no token")
    r = client.post(f"/api/locations/{loc}/field-corrections",
                    json={"field": "name", "value": "Nope"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "turnstile_failed"
    conn.rollback()
    n = conn.execute("SELECT count(*) AS n FROM field_corrections WHERE location_id=%s", (loc,)).fetchone()["n"]
    assert n == 0  # nothing written


@requires_db
def test_field_correction_short_name_rejected(conn, client):
    loc = _mk_location(conn, "Short name guard bin")
    r = _propose_field(client, loc, {"field": "name", "value": "X"}, "198.52.33.1")
    assert r.status_code == 422 and r.json()["error"]["code"] == "rejected"


@requires_db
def test_open_field_correction_is_surfaced_in_detail(conn, client):
    loc = _mk_location(conn, "Surfaced bin")
    _seed_engagement(client, loc, 2, start=34)  # Warm so it stays open and visible
    cid = _propose_field(client, loc, {"field": "name", "value": "Surfaced New Name"}, "198.52.34.99").json()["correction_id"]
    detail = client.get(f"/api/locations/{loc}").json()
    opens = {c["id"]: c for c in detail["open_field_corrections"]}
    assert cid in opens
    assert opens[cid]["field"] == "name" and opens[cid]["proposed_value"] == "Surfaced New Name"
    assert opens[cid]["required_support"] == 2


@requires_db
def test_orgs_endpoint_lists_active_org_names(conn, client):
    loc = _mk_location(conn, "Orgs listing bin")
    conn.execute("UPDATE locations SET org_name=%s, status='active' WHERE id=%s",
                 ("Goodwill Test Industries", loc))
    conn.commit()
    r = client.get("/api/orgs")
    assert r.status_code == 200
    assert "Goodwill Test Industries" in r.json()["orgs"]


# --- #5 Rating deselect -----------------------------------------------------
@requires_db
def test_clear_attribute_retracts_own_rating(conn, client):
    loc = _mk_location(conn, "Deselect bin")
    ip = "203.1.10.1"
    r = client.post(f"/api/locations/{loc}/attributes",
                    json={"attribute": "safety", "value": 2, "turnstile_token": TOK},
                    headers={"X-Real-IP": ip})
    assert r.status_code == 200 and r.json()["attributes"]["safety"]["count"] == 1
    d = client.request("DELETE", f"/api/locations/{loc}/attributes/safety",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": ip})
    assert d.status_code == 200, d.text
    agg = d.json()["attributes"]
    assert "safety" not in agg or agg["safety"]["count"] == 0


@requires_db
def test_clear_attribute_only_affects_caller(conn, client):
    loc = _mk_location(conn, "Deselect shared bin")
    client.post(f"/api/locations/{loc}/attributes",
                json={"attribute": "safety", "value": 3, "turnstile_token": TOK},
                headers={"X-Real-IP": "203.1.11.1"})
    client.post(f"/api/locations/{loc}/attributes",
                json={"attribute": "safety", "value": 1, "turnstile_token": TOK},
                headers={"X-Real-IP": "203.1.11.2"})
    # ip .1 clears only its own vote; ip .2's rating remains.
    d = client.request("DELETE", f"/api/locations/{loc}/attributes/safety",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": "203.1.11.1"})
    assert d.status_code == 200
    assert d.json()["attributes"]["safety"]["count"] == 1


@requires_db
def test_clear_unknown_attribute_422(conn, client):
    loc = _mk_location(conn, "Deselect bad attr bin")
    d = client.request("DELETE", f"/api/locations/{loc}/attributes/banana",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": "203.1.12.1"})
    assert d.status_code == 422 and d.json()["error"]["code"] == "bad_attribute"


@requires_db
def test_clear_attribute_missing_token_403(conn, client):
    loc = _mk_location(conn, "Deselect no token bin")
    d = client.request("DELETE", f"/api/locations/{loc}/attributes/safety",
                       json={}, headers={"X-Real-IP": "203.1.13.1"})
    assert d.status_code == 403 and d.json()["error"]["code"] == "turnstile_failed"


@requires_db
def test_clear_attribute_missing_location_404(conn, client):
    d = client.request("DELETE", "/api/locations/999000111/attributes/safety",
                       json={"turnstile_token": TOK}, headers={"X-Real-IP": "203.1.14.1"})
    assert d.status_code == 404 and d.json()["error"]["code"] == "not_found"


# --- #3 Pending-resurrect on resubmit --------------------------------------
@requires_db
@pytest.mark.owner_only  # endpoint works as app role; the DELETE self-clean below needs the owner
def test_resubmit_resurfaces_gated_pending_location(conn, client):
    """A fresh crowd location lands at confidence 20 / status 'pending' (invisible). Re-adding it
    from a different person resurfaces it (implicit confirm: 20 -> 25 -> active) rather than
    dead-ending as a duplicate of a pin the user can't see. Then it IS a visible duplicate."""
    name = "Resurfacing Test Donation Bin"
    lat, lon = 40.6011, -82.6011
    conn.execute("DELETE FROM locations WHERE name=%s", (name,))
    conn.execute("DELETE FROM pending_locations WHERE name=%s", (name,))
    conn.commit()

    # 1) First submit creates the gated location.
    r1 = client.post("/api/locations", json={
        "name": name, "org_type": "drop_bin", "address": {"city": "Testburg"},
        "lat": lat, "lon": lon, "turnstile_token": TOK}, headers={"X-Real-IP": "205.1.0.1"})
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["status"] == "promoted" and b1["location_id"] is not None
    loc_id = b1["location_id"]
    assert _row(conn, loc_id)["status"] == "pending"  # gated off the map by low confidence

    # 2) A different person re-adds the same spot -> resurfaced, and the implicit confirm activates it.
    r2 = client.post("/api/locations", json={
        "name": name, "org_type": "drop_bin", "address": {"city": "Testburg"},
        "lat": lat, "lon": lon, "turnstile_token": TOK}, headers={"X-Real-IP": "205.1.0.2"})
    assert r2.status_code == 200, r2.text
    b2 = r2.json()
    assert b2["status"] == "resurfaced"
    assert b2["location_id"] == loc_id and b2["duplicate_of"] == loc_id
    assert b2["now_active"] is True
    assert _row(conn, loc_id)["status"] == "active"

    # 3) Now that it's visible, a third re-add is a true duplicate (no new pin, not resurfaced).
    r3 = client.post("/api/locations", json={
        "name": name, "org_type": "drop_bin", "address": {"city": "Testburg"},
        "lat": lat, "lon": lon, "turnstile_token": TOK}, headers={"X-Real-IP": "205.1.0.3"})
    assert r3.status_code == 200, r3.text
    b3 = r3.json()
    assert b3["status"] == "duplicate" and b3["duplicate_of"] == loc_id
    assert b3["now_active"] is False
