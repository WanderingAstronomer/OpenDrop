"""P3 — property / invariant tests (Hypothesis). The locked HIGH-rigor phase.

Where the rest of the suite pins behaviour with hand-picked cases, this file states the backend's
core invariants and lets Hypothesis hunt for a counterexample across a wide input space. Every
invariant is grounded in the REAL head schema (the live function/view bodies were read out of the
migrated DB, not assumed):

  1. CONFIDENCE   — `recompute_confidence` is reproduced exactly (source + clamped crowd − staleness,
                    with the engagement-tiered deny-dominance cap), and the two monotonicity laws
                    hold: more confirms never LOWER confidence, more denies never RAISE it, result
                    always in [0, 100].
  2. MOVE-CAP     — a pin correction is accepted iff its target is within
                    `correction_max_move_m` (geography) of the IMMUTABLE origin; farther is 422
                    `move_too_far`. Measured from origin so a chain of small legal moves can't walk
                    a pin across the map.
  3. VISIBILITY   — export-view membership ⟺ (active ∧ redistributable); map membership ⟺ active;
                    redistributable ⟺ ≥1 ingest source. Hence export ⊆ map, always.
  4. GPS PRIVACY  — the corroboration path accepts only the boolean `gps_corroborated`, never device
                    coordinates, and the server never re-publishes that boolean in any response.

Runs DB-backed against the isolated `opendrop_test` DB (never live `opendrop`). Hypothesis drives
many examples through ONE function-scoped conn/client; they are deliberately NOT reset per example
(each example seeds its own fresh location, so there is no cross-example bleed that matters), which
is why the function_scoped_fixture health check is suppressed.
"""
import itertools
import math

from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from app.config import settings as app_settings
from app.models import CorrectionIn, CorrectionVoteIn

from conftest import requires_db  # noqa: E402

TOK = "dev-mock-token"  # any non-empty token passes the CF dev test secret
_CAP = app_settings.correction_max_move_m  # 2000 m; read from config, not hard-coded

# Distinct synthetic client IP per API write across the WHOLE module, so per-IP-per-day rate-limit
# caps (corrections=15/IP/day) can never bite a property test that posts hundreds of corrections.
_ips = itertools.count(1)


def _ip() -> str:
    n = next(_ips)
    return f"10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


def _prop(max_examples: int):
    """Standard Hypothesis settings for the DB-backed properties here."""
    return hsettings(max_examples=max_examples, deadline=None,
                     suppress_health_check=[HealthCheck.function_scoped_fixture])


# --- seeding helpers (direct DB writes — fast + deterministic) ---------------------------------

def _new_loc(conn, lat, lon, sources=("salvation_army",)):
    """Insert a location at (lat,lon) with the given source codes; return its id.

    A BEFORE-INSERT trigger anchors origin_geom = geom, and each source insert fires
    trg_after_source -> recompute_confidence, so a sourced location is already in steady state.
    """
    loc = conn.execute(
        "INSERT INTO locations (geom, name, org_type) "
        "VALUES (ST_SetSRID(ST_MakePoint(%s,%s),4326), %s, 'drop_bin') RETURNING id",
        (lon, lat, f"prop bin {next(_ips)}"),
    ).fetchone()["id"]
    for code in sources:
        conn.execute(
            "INSERT INTO location_sources (location_id, source_code, source_ref, source_geom) "
            "VALUES (%s,%s,%s, ST_SetSRID(ST_MakePoint(%s,%s),4326))",
            (loc, code, f"{code}/{loc}", lon, lat),
        )
    conn.commit()
    return loc


def _cast_votes(conn, loc, confirms, denies):
    """Insert `confirms` confirm + `denies` deny votes, each from a DISTINCT ip_hash (so they all
    count and engagement == confirms+denies). Every row fires trg_after_vote -> recompute."""
    for _ in range(confirms):
        conn.execute("INSERT INTO votes (location_id, vote, ip_hash) VALUES (%s,'confirm',%s)",
                     (loc, f"ipc/{loc}/{next(_ips)}"))
    for _ in range(denies):
        conn.execute("INSERT INTO votes (location_id, vote, ip_hash) VALUES (%s,'deny',%s)",
                     (loc, f"ipd/{loc}/{next(_ips)}"))
    conn.commit()


def _row(conn, loc):
    """Fresh snapshot of the recomputed state (rollback ends any open txn so API commits are seen)."""
    conn.rollback()
    return conn.execute(
        "SELECT confidence::float8 AS confidence, status, is_redistributable, "
        "location_engagement(%s) AS eng FROM locations WHERE id = %s",
        (loc, loc),
    ).fetchone()


def _expected_conf(weight, up, dn, eng):
    """Pure-Python mirror of recompute_confidence (head body, read from the live DB).

    Staleness is treated as ~0: on a freshly-seeded row last_verified_at is within seconds of now(),
    so 2*(age/30d) rounds away under round(.,2). Callers assert within a small tolerance to absorb it.
    """
    src = min(85, weight)
    crowd = max(-40, min(30, 5 * up - 8 * dn))
    conf = max(0, min(100, src + crowd))
    floor = 2 if eng < 3 else (4 if eng < 15 else 8)  # retire_deny_floor: Cold/Warm/Hot
    if dn >= floor and dn > up:                        # engagement-tiered deny-dominance cap
        conf = min(conf, 20)
    return round(conf, 2)


# ============================================================================================
# Invariant 1 — confidence
# ============================================================================================

@requires_db
@_prop(60)
@given(up=st.integers(0, 14), dn=st.integers(0, 14))
def test_confidence_reproduces_recompute_formula(conn, up, dn):
    """For a single salvation_army-sourced (weight 50) location, the stored confidence matches the
    reproduced formula exactly (within the tiny staleness tolerance) and stays in [0,100].

    Engagement is read back from the DB and fed into the expected value, so this isolates the
    recompute formula itself without re-deriving location_engagement."""
    loc = _new_loc(conn, 39.96, -82.99, ("salvation_army",))
    _cast_votes(conn, loc, up, dn)
    r = _row(conn, loc)
    expected = _expected_conf(50, up, dn, r["eng"])
    assert abs(r["confidence"] - expected) <= 0.05, (up, dn, r["eng"], r["confidence"], expected)
    assert 0.0 <= r["confidence"] <= 100.0


@requires_db
@_prop(40)
@given(up=st.integers(0, 10), dn=st.integers(0, 10), extra=st.integers(1, 5))
def test_more_confirms_never_lower_confidence(conn, up, dn, extra):
    """Monotone non-decreasing in confirms: adding confirmations can only hold or raise confidence
    (crowd rises, and a higher confirm count makes the deny-dominance cap LESS likely to fire)."""
    a = _new_loc(conn, 39.96, -82.99, ("salvation_army",))
    _cast_votes(conn, a, up, dn)
    b = _new_loc(conn, 39.96, -82.99, ("salvation_army",))
    _cast_votes(conn, b, up + extra, dn)
    ca, cb = _row(conn, a)["confidence"], _row(conn, b)["confidence"]
    assert cb >= ca - 0.05, (up, dn, extra, ca, cb)


@requires_db
@_prop(40)
@given(d=st.data())
def test_more_denies_never_raise_confidence_within_tier(conn, d):
    """Antitone in denies *within a fixed engagement tier*.

    NOT globally antitone, by design: adding a deny can bump a location into a higher engagement
    tier whose larger retire_deny_floor REMOVES the deny-dominance cap, so confidence can rise
    (e.g. Cold (0,2)->capped 20 vs Warm (0,3)->uncapped 26 — a busier spot needs more denies to
    retire). Hypothesis found exactly this counterexample to the naive claim. Held inside one tier
    the floor is constant, so more denies can only hold or lower confidence — which IS an invariant.
    Both points are pinned in the Warm band [3,14] (constant floor 4)."""
    eng_a = d.draw(st.integers(3, 13))            # Warm
    up = d.draw(st.integers(0, eng_a))
    dn = eng_a - up
    extra = d.draw(st.integers(1, 14 - eng_a))    # eng_b = eng_a+extra stays within Warm
    a = _new_loc(conn, 39.96, -82.99, ("salvation_army",))
    _cast_votes(conn, a, up, dn)
    b = _new_loc(conn, 39.96, -82.99, ("salvation_army",))
    _cast_votes(conn, b, up, dn + extra)
    ca, cb = _row(conn, a)["confidence"], _row(conn, b)["confidence"]
    assert cb <= ca + 0.05, (up, dn, extra, ca, cb)


@requires_db
def test_deny_dominance_caps_confidence_only_on_strict_majority(conn):
    """The retire mechanism: confidence is capped at ≤20 exactly when denies reach the tier floor
    AND STRICTLY exceed confirms. A bare tie is a dispute, not a retirement, so it is NOT capped —
    the strict `>` the head formula encodes."""
    for confirms, denies in [(0, 2), (0, 4), (7, 8)]:   # Cold/Warm/Hot, each dn>=floor and dn>up
        loc = _new_loc(conn, 39.96, -82.99, ("salvation_army",))
        _cast_votes(conn, loc, confirms, denies)
        assert _row(conn, loc)["confidence"] <= 20.0, (confirms, denies)
    tie = _new_loc(conn, 39.96, -82.99, ("salvation_army",))
    _cast_votes(conn, tie, 4, 4)                          # dn==up: NOT a retirement
    assert _row(conn, tie)["confidence"] > 20.0


@requires_db
def test_status_active_iff_confidence_at_least_25(conn):
    """The status gate tracks the same 25 threshold (cases chosen clear of the boundary so the
    sub-cent staleness term can't flip a razor-edge example)."""
    hi = _new_loc(conn, 39.96, -82.99, ("salvation_army",))          # weight 50 alone -> 50
    r = _row(conn, hi)
    assert r["confidence"] >= 25 and r["status"] == "active"
    _cast_votes(conn, hi, 0, 6)                                       # deny-dominance caps at 20
    r2 = _row(conn, hi)
    assert r2["confidence"] <= 20 and r2["status"] == "pending"
    lo = _new_loc(conn, 39.96, -82.99, ())                           # no ingest source
    _cast_votes(conn, lo, 1, 0)                                      # 1 confirm -> conf 5
    r3 = _row(conn, lo)
    assert r3["confidence"] < 25 and r3["status"] == "pending"


# ============================================================================================
# Invariant 2 — correction move-cap (measured from the immutable origin)
# ============================================================================================

_EARTH_M = 6371008.8  # mean Earth radius; spherical forward, good to ~0.5% of the WGS84 spheroid


def _dest(lat, lon, bearing_deg, dist_m):
    """Point at distance/bearing from (lat,lon) on a sphere."""
    br, lat1, lon1 = math.radians(bearing_deg), math.radians(lat), math.radians(lon)
    dr = dist_m / _EARTH_M
    lat2 = math.asin(math.sin(lat1) * math.cos(dr) + math.cos(lat1) * math.sin(dr) * math.cos(br))
    lon2 = lon1 + math.atan2(math.sin(br) * math.sin(dr) * math.cos(lat1),
                             math.cos(dr) - math.sin(lat1) * math.sin(lat2))
    return math.degrees(lat2), math.degrees(lon2)


@requires_db
@_prop(50)
@given(bearing=st.floats(0, 360, allow_nan=False, allow_infinity=False),
       near=st.floats(0, 1, allow_nan=False),    # fraction of (cap-100 m): always inside
       beyond=st.floats(0, 1, allow_nan=False))  # mapped to [cap+100, cap+38000]: always outside
def test_move_cap_accepts_within_rejects_beyond(conn, client, bearing, near, beyond):
    """A target within the cap is accepted; a target beyond it is 422 move_too_far. The 100 m guard
    band on each side keeps the spherical-vs-spheroid discrepancy (~10 m at 2 km) from flipping the
    verdict, so the property is decisive rather than flaky at the boundary."""
    lat, lon = 39.96, -82.99
    d_in = near * (_CAP - 100)
    d_out = _CAP + 100 + beyond * 38000

    loc = _new_loc(conn, lat, lon)
    nlat, nlon = _dest(lat, lon, bearing, d_in)
    r = client.post(f"/api/locations/{loc}/corrections",
                    json={"suggested_lat": nlat, "suggested_lon": nlon, "turnstile_token": TOK},
                    headers={"X-Real-IP": _ip()})
    assert r.status_code == 200, (d_in, r.status_code, r.text)

    loc2 = _new_loc(conn, lat, lon)
    flat, flon = _dest(lat, lon, bearing, d_out)
    r2 = client.post(f"/api/locations/{loc2}/corrections",
                     json={"suggested_lat": flat, "suggested_lon": flon, "turnstile_token": TOK},
                     headers={"X-Real-IP": _ip()})
    assert r2.status_code == 422, (d_out, r2.status_code, r2.text)
    assert r2.json()["error"]["code"] == "move_too_far", r2.text


@requires_db
def test_move_cap_origin_anchored_not_current_pin(conn, client):
    """The cap is measured from the IMMUTABLE origin, not the latest pin: a sequence of small legal
    moves can't be chained to walk a pin past the cap. After a ~1.5 km move applies, a second move
    that is within 2 km of the NEW pin but >2 km from origin is rejected."""
    lat, lon = 39.96, -82.99
    loc = _new_loc(conn, lat, lon)
    # First legal move ~1500 m east, on a Cold location it auto-applies and shifts geom.
    m1lat, m1lon = _dest(lat, lon, 90, 1500)
    r1 = client.post(f"/api/locations/{loc}/corrections",
                     json={"suggested_lat": m1lat, "suggested_lon": m1lon, "turnstile_token": TOK},
                     headers={"X-Real-IP": _ip()})
    assert r1.status_code == 200 and r1.json()["applied"] is True, r1.text
    # ~1500 m further east: ~1.5 km from the new pin (legal vs current) but ~3 km from origin.
    m2lat, m2lon = _dest(m1lat, m1lon, 90, 1500)
    r2 = client.post(f"/api/locations/{loc}/corrections",
                     json={"suggested_lat": m2lat, "suggested_lon": m2lon, "turnstile_token": TOK},
                     headers={"X-Real-IP": _ip()})
    assert r2.status_code == 422 and r2.json()["error"]["code"] == "move_too_far", r2.text


# ============================================================================================
# Invariant 3 — visibility (map ⊇ export)
# ============================================================================================

_INGEST = ("salvation_army", "osm", "usagain", "planet_aid", "wearable_collections", "crowd")
_ALL_SRC = _INGEST + ("goodwill",)  # goodwill is enrich_only -> never makes a row redistributable
_VBBOX = "-110.30,45.90,-109.90,46.30"  # empty Montana bbox; all visibility rows live at its center


@requires_db
@_prop(60)
@given(srcs=st.lists(st.sampled_from(_ALL_SRC), min_size=0, max_size=3, unique=True),
       up=st.integers(0, 10), dn=st.integers(0, 8))
def test_visibility_export_is_active_and_redistributable_subset_of_map(conn, client, srcs, up, dn):
    """For any source set + vote mix:
      * is_redistributable ⟺ the row has ≥1 ingest source;
      * export-view membership ⟺ (active ∧ redistributable);
      * map membership (min_confidence=0) ⟺ active ∨ (pending ∧ has a crowd source) — the map is the
        inclusive community view, so crowd-submitted pending pins show (badged unconfirmed) but
        ingest-only pending pins do not;
      * therefore export ⊆ map (export requires active, and every active pin is on the map).
    """
    lat, lon = 46.10, -110.10
    loc = _new_loc(conn, lat, lon, tuple(srcs))
    # Guarantee a recompute fires even in the no-source/no-vote corner (is_redistributable DEFAULTs
    # to true on raw insert; only a recompute sets it to reflect the real source set). A real
    # location always carries at least one signal, so forcing one here is faithful, not artificial.
    _cast_votes(conn, loc, up if (up + dn) else 1, dn)

    r = _row(conn, loc)
    has_ingest = any(s in _INGEST for s in srcs)
    assert r["is_redistributable"] == has_ingest, (srcs, r["is_redistributable"])

    in_export = conn.execute(
        "SELECT 1 FROM v_public_locations WHERE id = %s", (loc,)).fetchone() is not None
    assert in_export == (r["status"] == "active" and r["is_redistributable"])

    feats = client.get("/api/locations",
                       params={"bbox": _VBBOX, "cluster": "off", "min_confidence": 0}).json()
    on_map = loc in {f["properties"]["id"] for f in feats["features"]}
    has_crowd = "crowd" in srcs
    expect_map = r["status"] == "active" or (r["status"] == "pending" and has_crowd)
    assert on_map == expect_map, (r["status"], has_crowd, on_map)
    if on_map:
        prop = next(f["properties"] for f in feats["features"] if f["properties"]["id"] == loc)
        assert prop["unconfirmed"] == (r["status"] != "active")
    assert not (in_export and not on_map), "export must be a subset of the map"


# ============================================================================================
# Invariant 4 — GPS privacy
# ============================================================================================

def _coord_fields(model):
    return {n for n in model.model_fields if any(k in n for k in ("lat", "lon", "coord"))}


def test_corroboration_models_take_only_a_boolean_not_device_coordinates():
    """Static contract: the correction request models expose only the PUBLIC suggested pin as
    coordinates; the GPS signal is the boolean gps_corroborated. No field names a submitter's own
    device location, so device coordinates cannot even be transmitted to the server."""
    assert CorrectionIn.model_fields["gps_corroborated"].annotation is bool
    assert _coord_fields(CorrectionIn) == {"suggested_lat", "suggested_lon"}, _coord_fields(CorrectionIn)

    assert CorrectionVoteIn.model_fields["gps_corroborated"].annotation is bool
    assert _coord_fields(CorrectionVoteIn) == set(), _coord_fields(CorrectionVoteIn)


def _contains_key(obj, key):
    if isinstance(obj, dict):
        return key in obj or any(_contains_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(_contains_key(v, key) for v in obj)
    return False


@requires_db
@_prop(12)
@given(sub_gps=st.booleans(), vote_gps=st.booleans())
def test_gps_corroborated_is_never_re_published(conn, client, sub_gps, vote_gps):
    """Runtime complement to the frontend privacy contract: whatever gps_corroborated value a
    submitter or voter sends, the server never echoes that key in any response. The location is
    forced HOT (engagement 15 -> required_support 4) and the corroborating vote is a REJECTION, so
    the correction stays OPEN and actually appears in the detail payload — proving the assertion
    runs against a present correction, not a vacuously empty list."""
    lat, lon = 39.96, -82.99
    loc = _new_loc(conn, lat, lon, ("salvation_army",))
    _cast_votes(conn, loc, 15, 0)  # HOT: required_support 4, so one submitter (≤2) can't auto-apply

    pr = client.post(f"/api/locations/{loc}/corrections",
                     json={"suggested_lat": lat + 0.0006, "suggested_lon": lon,
                           "gps_corroborated": sub_gps, "turnstile_token": TOK},
                     headers={"X-Real-IP": _ip()})
    assert pr.status_code == 200, pr.text
    assert not _contains_key(pr.json(), "gps_corroborated"), pr.json()
    corr_id = pr.json()["correction_id"]

    vr = client.post(f"/api/corrections/{corr_id}/vote",
                     json={"confirm": False, "gps_corroborated": vote_gps, "turnstile_token": TOK},
                     headers={"X-Real-IP": _ip()})
    assert vr.status_code == 200, vr.text
    assert not _contains_key(vr.json(), "gps_corroborated"), vr.json()

    detail = client.get(f"/api/locations/{loc}").json()
    assert detail["open_corrections"], "expected the open correction to be listed"
    assert not _contains_key(detail, "gps_corroborated"), detail
