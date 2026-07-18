import re

import httpx

from .config import settings

UA = "OpenDrop/0.1 (civic open-data map; +https://github.com/WanderingAstronomer/OpenDrop)"

# USPS secondary-unit designators (Appendix C2, common subset). Nominatim's STRUCTURED `street`
# matcher is brittle: appending a unit — "3990 Broadway Suite 100" — makes it return ZERO results
# for an address that resolves cleanly without it (verified against live Nominatim). We geocode with
# the unit stripped; the caller keeps the full line for storage/display. The unit never contributes
# to the coordinate, so dropping it only ever helps.
_UNIT_DESIGNATORS = (
    "apt", "apartment", "unit", "suite", "ste", "bldg", "building", "fl", "floor",
    "rm", "room", "dept", "department", "lot", "trlr", "trailer", "spc", "space",
    "hangar", "hngr", "slip", "pier", "stop", "no", "num", "number",
)
# A trailing "<sep> DESIGNATOR [value]" clause (e.g. " Suite 100", ", Apt 2B", " Unit").
_UNIT_RE = re.compile(
    r"[\s,]+(?:" + "|".join(_UNIT_DESIGNATORS) + r")\b\.?\s*[\w-]*\s*$",
    re.IGNORECASE,
)
# A trailing "#3" / "# 3" clause, which the keyword list can't express.
_HASH_UNIT_RE = re.compile(r"[\s,]*#\s*[\w-]+\s*$")


def strip_unit(line: str | None) -> str | None:
    """Drop a trailing secondary-unit clause (Apt/Suite/Unit/#…) from a street line for geocoding.
    Returns the cleaned street, or the ORIGINAL if stripping would leave it empty (so a line that is
    *only* a unit still gets its shot at the free-text fallback rather than becoming blank)."""
    if not line:
        return line
    s = _HASH_UNIT_RE.sub("", line)
    s = _UNIT_RE.sub("", s)
    s = s.strip()
    return s or line

# Tiny in-process cache for free-text search (respects Nominatim's no-heavy-use policy).
_SEARCH_CACHE: dict[str, list] = {}
_SEARCH_CACHE_MAX = 512


async def search(q: str, limit: int = 5) -> list[dict]:
    """Free-text place search via Nominatim → list of {name, lat, lon, bbox}. Cached, never raises."""
    key = q.strip().lower()
    if key in _SEARCH_CACHE:
        return _SEARCH_CACHE[key]
    params = {"format": "jsonv2", "limit": str(limit), "countrycodes": "us", "q": q, "addressdetails": "0"}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": UA}) as client:
            resp = await client.get(settings.nominatim_url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for d in data:
        try:
            bb = d.get("boundingbox")  # Nominatim order: [south, north, west, east]
            out.append({
                "name": d.get("display_name"),
                "lat": float(d["lat"]),
                "lon": float(d["lon"]),
                "bbox": {"south": float(bb[0]), "north": float(bb[1]), "west": float(bb[2]), "east": float(bb[3])} if bb else None,
            })
        except (KeyError, ValueError, IndexError, TypeError):
            continue
    if len(_SEARCH_CACHE) < _SEARCH_CACHE_MAX:
        _SEARCH_CACHE[key] = out
    return out


async def reverse(lat: float, lon: float) -> dict | None:
    """Reverse-geocode (lat, lon) → {line, city, state, postal_code, display_name} or None.
    Powers drop-a-pin address back-fill. Never raises."""
    url = settings.nominatim_url.replace("/search", "/reverse")
    params = {"format": "jsonv2", "lat": str(lat), "lon": str(lon), "addressdetails": "1", "zoom": "18"}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": UA}) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None
    addr = data.get("address")
    if not isinstance(addr, dict):
        return None
    road = addr.get("road") or addr.get("pedestrian") or addr.get("footway") or addr.get("path")
    line = " ".join(p for p in (addr.get("house_number"), road) if p) or None
    city = (addr.get("city") or addr.get("town") or addr.get("village")
            or addr.get("hamlet") or addr.get("suburb") or addr.get("county"))
    iso = addr.get("ISO3166-2-lvl4") or ""  # e.g. "US-OH" for US states
    state = iso.split("-")[-1].upper() if "-" in iso and len(iso.split("-")[-1]) == 2 else None
    return {"line": line, "city": city, "state": state,
            "postal_code": addr.get("postcode"), "display_name": data.get("display_name")}


async def _query(params: dict) -> tuple[float, float] | None:
    """One Nominatim request → (lat, lon) of the first hit, or None. Never raises."""
    try:
        async with httpx.AsyncClient(timeout=15, headers={"User-Agent": UA}) as client:
            resp = await client.get(settings.nominatim_url, params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    try:
        return float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, ValueError, IndexError, TypeError):
        return None


async def geocode(line=None, city=None, state=None, postal_code=None) -> tuple[float, float] | None:
    """Structured Nominatim geocode, with a free-text fallback → (lat, lon) or None. Never raises.

    Coordinates-first: a geocode miss is meant to be rare, because two brittleness traps are handled:
      1. a trailing unit (Apt/Suite/#) is stripped from `street` — Nominatim's structured matcher
         returns nothing for an otherwise-valid address when a unit is appended;
      2. if the structured query still finds nothing, we retry ONCE as a free-text `q` search, which
         is far more forgiving of field ordering and abbreviations. The extra call only fires on a
         miss, so the common (clean-address) path is still a single request."""
    street = strip_unit(line)
    params = {"format": "jsonv2", "limit": "1", "countrycodes": "us"}
    if street:
        params["street"] = street
    if city:
        params["city"] = city
    if state:
        params["state"] = state
    if postal_code:
        params["postalcode"] = postal_code
    if len(params) == 3:  # only the 3 constant params => nothing to geocode
        return None
    hit = await _query(params)
    if hit:
        return hit
    # Free-text fallback: reassemble the (unit-stripped) parts into one `q` string.
    q = ", ".join(p for p in (street, city, state, postal_code) if p)
    if not q:
        return None
    return await _query({"format": "jsonv2", "limit": "1", "countrycodes": "us", "q": q})
