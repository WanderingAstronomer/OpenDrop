import httpx

from .config import settings

UA = "OpenDrop/0.1 (civic open-data map; +https://github.com/WanderingAstronomer/OpenDrop)"

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


async def geocode(line=None, city=None, state=None, postal_code=None) -> tuple[float, float] | None:
    """Structured Nominatim geocode → (lat, lon) or None. Never raises."""
    params = {"format": "jsonv2", "limit": "1", "countrycodes": "us"}
    if line:
        params["street"] = line
    if city:
        params["city"] = city
    if state:
        params["state"] = state
    if postal_code:
        params["postalcode"] = postal_code
    if len(params) == 3:  # only the 3 constant params => nothing to geocode
        return None
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
    except (KeyError, ValueError, IndexError):
        return None
