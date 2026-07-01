"""Frontend GPS-privacy contract, enforced in CI.

OpenDrop's core privacy promise: the user's device coordinates are computed on-device and NEVER sent
to the server — the API only ever receives the resulting boolean `gps_corroborated`. These tests
read the shipped frontend JS and fail the build if a refactor ever lets a coordinate reach a network
call. They are static (no browser needed) so they run in the normal pytest job.

Invariants:
  1. geolocation is used in exactly two on-device modules (map-centering + corroboration);
  2. every module that performs a network request is free of any device-coordinate reference;
  3. the corroboration check resolves a haversine<=radius boolean, not coordinates.
"""
import re
from pathlib import Path

JS_DIR = Path(__file__).resolve().parents[2] / "frontend" / "js"

# Tokens that indicate raw device geolocation (NOT the user-dragged suggested_lat/lon pin, which is
# an explicit, coarse, user action and is allowed to be sent).
GEO_API = ("getCurrentPosition", "navigator.geolocation")
COORD_REFS = ("coords.latitude", "coords.longitude")

# Every module allowed to read the device's GPS. Each entry is here BECAUSE it was reviewed against
# the privacy contract — a new module appearing in this set should fail the build until a human
# confirms it doesn't silently transmit coordinates. The three vetted uses:
#   geo.js        — "locate me": coords used only to center the Leaflet map (never sent).
#   submit.js     — "snap new pin to my location": the user is deliberately contributing the public
#                   location of a donation bin (draggable, reverse-geocoded, user-initiated). The
#                   coordinate IS sent — that's the point — but as an explicit contribution, not
#                   silent tracking.
#   corrections.js — "am I standing here?" corroboration: computes distance on-device and resolves a
#                   BOOLEAN; the raw fix never leaves the callback (enforced by the boolean test below).
GEO_ALLOWLIST = {"geo.js", "submit.js", "corrections.js"}


def _all_js() -> dict[str, str]:
    files = {p.name: p.read_text(encoding="utf-8") for p in JS_DIR.glob("*.js")}
    assert files, f"no frontend JS found under {JS_DIR}"
    return files


def test_geolocation_users_match_the_reviewed_allowlist():
    files = _all_js()
    users = {n for n, s in files.items() if any(t in s for t in GEO_API)}
    unexpected = users - GEO_ALLOWLIST
    assert not unexpected, (
        f"module(s) {sorted(unexpected)} now read device GPS but aren't in the reviewed privacy "
        f"allowlist. Add them only after confirming they don't silently transmit coordinates, and "
        f"document why in GEO_ALLOWLIST."
    )


def test_network_modules_never_reference_device_coordinates():
    files = _all_js()
    network = {n for n, s in files.items() if re.search(r"\bfetch\s*\(", s)}
    assert network, "expected at least one module to perform fetch()"
    for n in sorted(network):
        s = files[n]
        for bad in GEO_API + COORD_REFS:
            assert bad not in s, (
                f"{n} performs network requests AND references {bad!r} — this would let device "
                f"coordinates reach the server, violating the GPS privacy contract"
            )


def test_corroboration_resolves_a_boolean_not_coordinates():
    s = _all_js()["corrections.js"]
    assert "gps_corroborated" in s, "corrections.js must send the gps_corroborated boolean"
    assert re.search(r"resolve\(\s*haversine\([^)]*\)\s*<=", s), (
        "the geolocation callback must resolve `haversine(...) <= radius` (a boolean); resolving "
        "raw coordinates would leak them out of the on-device scope"
    )
