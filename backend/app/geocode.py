import httpx

from .config import settings

UA = "OpenDrop/0.1 (civic open-data map; +https://github.com/WanderingAstronomer/OpenDrop)"


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
