"""Wikimedia Commons "Picture of the Day" proxy (GET /api/potd).

The frontend uses one shared daily image in two places — a photo-section placeholder for bins
with no community photos, and a first-visit welcome hero — both of which REQUIRE the Commons
license attribution to ride along. This module is the single server-side fetch: it calls the
Wikipedia REST "featured feed" for today's UTC date, extracts the `image` (POTD) block, and
reshapes it into a flat, attribution-complete payload.

Failure is never fatal: on ANY problem (network, non-200, missing `image`, parse error) the
endpoint returns {"available": false} with HTTP 200 so the client can silently render nothing.

Fetched at most once per UTC day (module-level cache keyed by the date string); the date rolling
over triggers a refetch. Negative results are cached briefly too, so a Wikimedia outage can't turn
into a fetch-per-request stampede. Mirrors the httpx client/timeout/User-Agent pattern in
app.geocode (Wikimedia requires a descriptive User-Agent)."""
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter

# Same descriptive UA as the geocode proxy — Wikimedia's API policy requires one identifying the
# client + a contact URL, and rejects generic/absent agents.
UA = "OpenDrop/0.1 (civic open-data map; +https://github.com/WanderingAstronomer/OpenDrop)"
FEED_URL = "https://en.wikipedia.org/api/rest_v1/feed/featured/{y:04d}/{m:02d}/{d:02d}"

router = APIRouter()

# Module-level single-slot cache: {"date": "YYYY-MM-DD", "payload": {...}, "at": monotonic-ts}.
# One image per UTC day, shared by every request that day.
_cache: dict | None = None
# A negative (available:false) result is only trusted for this long, so a transient Wikimedia
# outage self-heals within minutes instead of pinning "unavailable" for the whole UTC day. A good
# result is cached until the date rolls over (checked by _today_utc()).
_NEGATIVE_TTL_S = 600


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _shape(image: dict, date: str) -> dict:
    """Flatten Wikimedia's nested POTD `image` block into our attribution-complete payload.
    Prefer artist/description `.text` over `.html` — we never inject upstream HTML into the DOM."""
    return {
        "available": True,
        "date": date,
        "title": image.get("title"),
        "image_url": (image.get("image") or {}).get("source"),
        "thumb_url": (image.get("thumbnail") or {}).get("source"),
        "source_url": image.get("file_page"),
        "artist": (image.get("artist") or {}).get("text"),
        "license": (image.get("license") or {}).get("type"),
        "license_url": (image.get("license") or {}).get("url"),
        "description": (image.get("description") or {}).get("text"),
    }


async def _fetch(date: str) -> dict:
    """Fetch + shape today's POTD, or {"available": false} on ANY failure. Never raises."""
    now = datetime.now(timezone.utc)
    url = FEED_URL.format(y=now.year, m=now.month, d=now.day)
    try:
        async with httpx.AsyncClient(timeout=6, headers={"User-Agent": UA}) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        image = data.get("image") if isinstance(data, dict) else None
        if not isinstance(image, dict):
            return {"available": False}
        return _shape(image, date)
    except Exception:  # noqa: BLE001 — degrade gracefully on every failure mode
        return {"available": False}


@router.get("/potd")
async def potd():
    """Today's Wikimedia Commons Picture of the Day (flattened + attribution), or {available:false}.
    Cached once per UTC day; negative results cached briefly. Always HTTP 200."""
    global _cache
    date = _today_utc()
    if _cache and _cache["date"] == date:
        payload = _cache["payload"]
        # Serve a cached hit; a cached MISS only stands for _NEGATIVE_TTL_S before we retry upstream.
        if payload.get("available") or (time.monotonic() - _cache["at"]) < _NEGATIVE_TTL_S:
            return payload
    payload = await _fetch(date)
    _cache = {"date": date, "payload": payload, "at": time.monotonic()}
    return payload
