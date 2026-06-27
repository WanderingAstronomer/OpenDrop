#!/usr/bin/env python3
"""
OpenDrop deduplication sample / threshold-scoping harness.

Pairs Set A (OSM Columbus donation features) against Set B (live Goodwill
national-locator feed) by (geographic distance, normalized-name similarity)
across a sweep of thresholds, so we can empirically pick merge thresholds.

Design constraints from the task:
  - stdlib only is REQUIRED to run (math haversine + difflib.SequenceMatcher,
    plus a token-set Jaccard). rapidfuzz is used ONLY if importable, purely as
    a cross-check column; the recommendation is computed from the stdlib scorer.

Inputs (absolute paths, same dir as this script):
  - osm_columbus_flat.json  : Set A, flattened {osm,class,name,operator,lat,lon,hours,addr}
  - org_feed_columbus.json  : Set B, live Goodwill feed (wrapper with .records)

Output: prints a candidate-pair sweep table and the per-pair best scores.
Run:  python dedup_sample.py
"""

import json
import math
import os
import re
import difflib

try:
    from rapidfuzz import fuzz as _rf_fuzz
    HAVE_RAPIDFUZZ = True
except Exception:
    HAVE_RAPIDFUZZ = False

HERE = os.path.dirname(os.path.abspath(__file__))
OSM_PATH = os.path.join(HERE, "osm_columbus_flat.json")
ORG_PATH = os.path.join(HERE, "org_feed_columbus.json")

# ----------------------------------------------------------------------------
# Geo
# ----------------------------------------------------------------------------
def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in meters (stdlib math only)."""
    R = 6371008.8  # mean Earth radius, meters (IUGG)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2)
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))

# ----------------------------------------------------------------------------
# Name normalization
# ----------------------------------------------------------------------------
# Tokens/phrases that carry no identity signal for a donation site.
_STOP_PHRASES = [
    "donation center", "donation centre", "attended donation center",
    "drop off center", "drop-off center", "family store", "thrift store",
    "thrift shop", "retail store", "outlet store", "outlet",
    "shopping center", "store", "shop", "donation", "center", "centre",
]
_STOP_WORDS = {"the", "inc", "llc", "co", "of", "a", "and", "&", "at",
               "dr", "rd", "st", "ave", "blvd", "ln", "way"}

# Brand canonicalization: many spellings -> one token.
_BRAND_CANON = [
    (re.compile(r"\bgoodwill\b.*", re.I), "goodwill"),
    (re.compile(r"\bsalvation\s*army\b.*", re.I), "salvation army"),
    (re.compile(r"\bvolunteer'?s?\s+of\s+america\b.*", re.I), "volunteers of america"),
    (re.compile(r"\bhabitat\s+for\s+humanity\b.*", re.I), "habitat for humanity"),
    (re.compile(r"\bohio\s+thrift\b.*", re.I), "ohio thrift"),
]

def normalize_name(name, operator=""):
    """Lowercase, strip registered/trademark marks and noise phrases,
    canonicalize national brands, collapse whitespace.

    operator is folded in because OSM often leaves name='' but sets
    operator='Goodwill', and the org feed names a specific store while the
    operator is the brand. Brand match is the strongest signal we have."""
    raw = (name or "").strip()
    op = (operator or "").strip()

    # Brand canonicalization runs on name-or-operator: if either says Goodwill,
    # the canonical brand token is goodwill.
    probe = (raw + " " + op).lower()
    for rx, canon in _BRAND_CANON:
        if rx.search(probe):
            return canon

    s = raw.lower()
    s = s.replace("®", " ").replace("™", " ")  # (R) (TM)
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    # strip multi-word noise phrases (longest first)
    for ph in sorted(_STOP_PHRASES, key=len, reverse=True):
        s = re.sub(r"\b" + re.escape(ph) + r"\b", " ", s)
    toks = [t for t in s.split() if t and t not in _STOP_WORDS]
    return " ".join(toks).strip()

def name_tokens(norm):
    return set(norm.split()) if norm else set()

# ----------------------------------------------------------------------------
# Name similarity
# ----------------------------------------------------------------------------
def seqmatch(a, b):
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()

def token_set_ratio(a, b):
    """Order-independent token overlap (Jaccard on token sets)."""
    sa, sb = name_tokens(a), name_tokens(b)
    if not sa and not sb:
        return 1.0
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union

def name_similarity(a, b):
    """Stdlib composite name score in [0,1]: max of char-ratio and token-set.
    Taking the max means 'Goodwill' vs 'Goodwill Columbus' (token subset)
    and 'Salvation Army' vs 'The Salvation Army Family Store' both score high
    after normalization, while unrelated names stay low."""
    return max(seqmatch(a, b), token_set_ratio(a, b))

# ----------------------------------------------------------------------------
# Load data
# ----------------------------------------------------------------------------
def load_sets():
    osm = json.load(open(OSM_PATH, encoding="utf-8"))
    setA = []
    for r in osm:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        setA.append({
            "id": r["osm"],
            "name": r.get("name", ""),
            "operator": r.get("operator", ""),
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "addr": r.get("addr", ""),
            "norm": normalize_name(r.get("name", ""), r.get("operator", "")),
        })
    org = json.load(open(ORG_PATH, encoding="utf-8"))
    records = org["records"] if isinstance(org, dict) and "records" in org else org
    setB = []
    for r in records:
        setB.append({
            "id": r["org_id"],
            "name": r.get("name", ""),
            "operator": r.get("operator", "Goodwill"),
            "lat": float(r["lat"]),
            "lon": float(r["lon"]),
            "addr": " ".join(str(r.get(k, "")) for k in ("street", "city", "state", "postal")).strip(),
            "norm": normalize_name(r.get("name", ""), r.get("operator", "Goodwill")),
        })
    return setA, setB

# ----------------------------------------------------------------------------
# Pairing
# ----------------------------------------------------------------------------
def all_pair_scores(setA, setB):
    """Cartesian scoring (small N). Returns list of dicts per A x B pair."""
    pairs = []
    for a in setA:
        for b in setB:
            d = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
            sim = name_similarity(a["norm"], b["norm"])
            rf = None
            if HAVE_RAPIDFUZZ:
                rf = _rf_fuzz.token_set_ratio(a["norm"], b["norm"]) / 100.0
            pairs.append({
                "a": a, "b": b, "dist_m": d,
                "name_sim": sim, "rf_sim": rf,
            })
    return pairs

def sweep(pairs, dist_thresholds, name_thresholds):
    """For each (dist, name) cell, count candidate pairs that pass BOTH gates."""
    grid = {}
    for dt in dist_thresholds:
        for nt in name_thresholds:
            cand = [p for p in pairs if p["dist_m"] <= dt and p["name_sim"] >= nt]
            grid[(dt, nt)] = cand
    return grid

# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
DIST_THRESHOLDS = [50, 100, 150, 300, 500]
NAME_THRESHOLDS = [0.4, 0.6, 0.8]

def main():
    setA, setB = load_sets()
    print(f"# Set A (OSM): {len(setA)} features  |  Set B (Goodwill feed): {len(setB)} records")
    print(f"# rapidfuzz available: {HAVE_RAPIDFUZZ}")

    pairs = all_pair_scores(setA, setB)
    grid = sweep(pairs, DIST_THRESHOLDS, NAME_THRESHOLDS)

    print("\n## Candidate-pair counts by (distance m x name-sim) gate")
    header = "dist\\name | " + " | ".join(f"{nt:>4}" for nt in NAME_THRESHOLDS)
    print(header)
    print("-" * len(header))
    for dt in DIST_THRESHOLDS:
        row = f"{dt:>9} | " + " | ".join(f"{len(grid[(dt,nt)]):>4}" for nt in NAME_THRESHOLDS)
        print(row)

    # Best candidate per OSM-A record (nearest within 500 m, any name score)
    print("\n## Per-A nearest Set-B candidate within 500 m (for manual adjudication)")
    print(f"{'OSM id':18} | {'A name/op':28} | {'B name':30} | {'dist_m':>7} | nsim | rf")
    print("-" * 110)
    by_a = {}
    for p in pairs:
        aid = p["a"]["id"]
        if aid not in by_a or p["dist_m"] < by_a[aid]["dist_m"]:
            by_a[aid] = p
    for aid, p in sorted(by_a.items(), key=lambda kv: kv[1]["dist_m"]):
        if p["dist_m"] > 500:
            continue
        a, b = p["a"], p["b"]
        alabel = (a["name"] or a["operator"] or "-")[:28]
        rf = f"{p['rf_sim']:.2f}" if p["rf_sim"] is not None else " - "
        print(f"{aid:18} | {alabel:28} | {b['name'][:30]:30} | {p['dist_m']:7.1f} | {p['name_sim']:.2f} | {rf}")

    # Dump the <=500m candidate set for the report
    out = []
    for p in pairs:
        if p["dist_m"] <= 500 and p["name_sim"] >= 0.4:
            out.append({
                "osm": p["a"]["id"], "osm_name": p["a"]["name"], "osm_op": p["a"]["operator"],
                "org": p["b"]["id"], "org_name": p["b"]["name"],
                "dist_m": round(p["dist_m"], 1),
                "name_sim": round(p["name_sim"], 3),
                "rf_sim": round(p["rf_sim"], 3) if p["rf_sim"] is not None else None,
            })
    out.sort(key=lambda r: r["dist_m"])
    cand_path = os.path.join(HERE, "dedup_candidates.json")
    json.dump(out, open(cand_path, "w"), indent=2)
    print(f"\n# Wrote {len(out)} candidate pairs (<=500m & nsim>=0.4) to {cand_path}")

if __name__ == "__main__":
    main()
