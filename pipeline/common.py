"""Shared, pure helpers used by BOTH the pipeline and the API submit-time dedup.

Mirrors the SQL functions in migrations/0001_init.sql (normalize_name, normalize_house_number)
and implements the Phase-1-validated dedup primitives (brand canonicalization, name similarity).
No DB or network here — keep it importable everywhere.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from math import asin, cos, radians, sin, sqrt

_NON_ALNUM = re.compile(r"[^a-z0-9 ]")
_WS = re.compile(r"\s+")
_LEADING_NUM = re.compile(r"^\s*([0-9]+)")


def normalize_name(txt: str | None) -> str:
    if not txt:
        return ""
    return _WS.sub(" ", _NON_ALNUM.sub(" ", txt.lower())).strip()


def normalize_house_number(addr: str | None) -> str | None:
    if not addr:
        return None
    m = _LEADING_NUM.match(addr)
    return m.group(1) if m else None


# Canonical brand tokens. Substring match against normalized org_name/operator/brand/name.
# Longest needles first so e.g. "society of st vincent de paul" wins before "st vincent de paul".
_BRANDS: list[tuple[str, str]] = sorted(
    [
        ("goodwill", "goodwill"),
        ("salvation army", "salvation_army"),
        ("volunteers of america", "volunteers_of_america"),
        ("habitat for humanity", "habitat"),
        ("restore", "habitat"),
        ("society of st vincent de paul", "svdp"),
        ("st vincent de paul", "svdp"),
        ("saint vincent de paul", "svdp"),
        ("planet aid", "planet_aid"),
        ("usagain", "usagain"),
        ("value village", "savers"),
        ("savers", "savers"),
        ("greendrop", "greendrop"),
        ("american red cross", "red_cross"),
        ("purple heart", "purple_heart"),
        ("ohio thrift", "ohio_thrift"),
    ],
    key=lambda kv: -len(kv[0]),
)


def brand_key(*candidates: str | None) -> str | None:
    """Canonical brand token from any candidate string, or None (unbranded — e.g. most bins)."""
    for c in candidates:
        norm = normalize_name(c)
        if not norm:
            continue
        for needle, token in _BRANDS:
            if needle in norm:
                return token
    return None


def _token_set_jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def name_sim(a: str | None, b: str | None) -> float:
    """max(SequenceMatcher ratio, token-set Jaccard) over normalized names.
    Two empty names score 0.0 (closes the empty-string-name over-merge trap)."""
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return 0.0
    return max(SequenceMatcher(None, na, nb).ratio(), _token_set_jaccard(na, nb))


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371008.8  # mean Earth radius (m), matches PostGIS geography
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def brand_equal(a: str | None, b: str | None) -> bool:
    return a is not None and a == b
