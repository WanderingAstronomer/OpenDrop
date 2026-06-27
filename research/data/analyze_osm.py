#!/usr/bin/env python3
"""Phase 1 OSM Data Audit analysis for the Columbus metro Overpass pull.

Reads osm_columbus.json and reports feature-class counts, tag completeness,
and schema gaps. Pure stdlib so it runs anywhere.
"""
import json
from collections import Counter

with open("osm_columbus.json", encoding="utf-8") as f:
    data = json.load(f)

elements = data.get("elements", [])


def classify(tags):
    if tags.get("shop") == "charity":
        return "shop=charity"
    if tags.get("shop") == "second_hand":
        return "shop=second_hand"
    if tags.get("amenity") == "recycling" and "recycling:clothes" in tags:
        return "recycling:clothes"
    if tags.get("amenity") == "recycling" and "recycling:shoes" in tags:
        return "recycling:shoes"
    return "other"


# Fields we care about for OpenDrop's canonical location record.
KEY_FIELDS = [
    "name", "opening_hours", "collection_times",
    "addr:housenumber", "addr:street", "addr:city", "addr:postcode",
    "operator", "brand", "website", "contact:website", "phone", "contact:phone",
]

by_class = Counter()
geom_types = Counter()
field_present = {f: 0 for f in KEY_FIELDS}
field_by_class = {}
operators = Counter()
brands = Counter()
recycling_clothes_values = Counter()
has_any_address = 0
has_any_hours = 0

rows = []
for el in elements:
    tags = el.get("tags", {})
    cls = classify(tags)
    by_class[cls] += 1
    geom_types[el["type"]] += 1
    field_by_class.setdefault(cls, {f: 0 for f in KEY_FIELDS})
    for f in KEY_FIELDS:
        if tags.get(f):
            field_present[f] += 1
            field_by_class[cls][f] += 1
    if tags.get("operator"):
        operators[tags["operator"]] += 1
    if tags.get("brand"):
        brands[tags["brand"]] += 1
    if "recycling:clothes" in tags:
        recycling_clothes_values[tags["recycling:clothes"]] += 1
    if any(tags.get(f) for f in ("addr:housenumber", "addr:street", "addr:city", "addr:postcode")):
        has_any_address += 1
    if tags.get("opening_hours") or tags.get("collection_times"):
        has_any_hours += 1

    lat = el.get("lat") or (el.get("center") or {}).get("lat")
    lon = el.get("lon") or (el.get("center") or {}).get("lon")
    rows.append({
        "osm": f"{el['type']}/{el['id']}",
        "class": cls,
        "name": tags.get("name", ""),
        "operator": tags.get("operator", tags.get("brand", "")),
        "lat": lat, "lon": lon,
        "hours": tags.get("opening_hours", tags.get("collection_times", "")),
        "addr": " ".join(filter(None, [tags.get("addr:housenumber",""), tags.get("addr:street",""), tags.get("addr:city","")])),
    })

n = len(elements)
print(f"TOTAL ELEMENTS: {n}\n")
print("BY FEATURE CLASS:")
for k, v in by_class.most_common():
    print(f"  {k:20s} {v}")
print("\nBY GEOMETRY TYPE:")
for k, v in geom_types.most_common():
    print(f"  {k:10s} {v}")
print("\nTAG COMPLETENESS (count / pct of all elements):")
for f in KEY_FIELDS:
    c = field_present[f]
    print(f"  {f:22s} {c:3d}  {100*c/n:5.1f}%")
print(f"\n  has ANY address field   {has_any_address:3d}  {100*has_any_address/n:5.1f}%")
print(f"  has ANY hours/collection {has_any_hours:3d}  {100*has_any_hours/n:5.1f}%")

print("\nTAG COMPLETENESS BY CLASS (name / hours-or-collection / address):")
for cls in by_class:
    fb = field_by_class[cls]
    tot = by_class[cls]
    # recompute hours/address per class
    nm = fb["name"]
    print(f"  {cls:20s} n={tot:2d}  name={nm}/{tot}  opening_hours={fb['opening_hours']}  collection_times={fb['collection_times']}  street={fb['addr:street']}")

print("\nrecycling:clothes VALUES:")
for k, v in recycling_clothes_values.most_common():
    print(f"  {k:10s} {v}")

print("\nTOP OPERATORS:")
for k, v in operators.most_common(15):
    print(f"  {v:2d}  {k}")
print("\nTOP BRANDS:")
for k, v in brands.most_common(15):
    print(f"  {v:2d}  {k}")

# Write a flattened CSV-ish table for the dedup task to consume later.
with open("osm_columbus_flat.json", "w", encoding="utf-8") as f:
    json.dump(rows, f, indent=2)
print(f"\nWrote {len(rows)} flattened rows -> osm_columbus_flat.json")
