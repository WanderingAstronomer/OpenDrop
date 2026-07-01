#!/usr/bin/env python3
"""Generate ``pipeline/data/us_zips.csv`` — the vendored, offline ZIP -> state + coordinates
table that drives the data-driven national regions (see ``pipeline/regions.py``).

Provenance
----------
Built from the `zipcodes` PyPI package (https://pypi.org/project/zipcodes/), which bundles a
public-domain US ZIP dataset derived from US Census / USPS data. We keep only the **50 states
+ DC** (territories PR/GU/VI/AS/MP + FM/MH/PW and the military APO/FPO ranges AA/AE/AP are
dropped — the donation sources we sweep don't list bins/stores there), emit ``zip,state,lat,lon``
sorted by ZIP, and **commit the result** so the runtime never needs network access or the
package. This script is the audit trail, not a runtime dependency.

Reproduce (writes the CSV in place; nothing is installed on the host):

    docker run --rm -v "$PWD:/src" -w /src python:3.12-slim \
      sh -c "pip install -q zipcodes && python pipeline/data/build_us_zips.py"
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

# 50 states + DC. Everything else the source carries (territories, FM/MH/PW compacts, and the
# AA/AE/AP military ranges) is intentionally excluded — see the module docstring.
US_STATES = frozenset(
    """AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT
       NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC""".split()
)


def main() -> int:
    try:
        import zipcodes
    except ModuleNotFoundError:
        print("This generator needs the `zipcodes` package: pip install zipcodes", file=sys.stderr)
        return 2

    out = Path(__file__).resolve().parent / "us_zips.csv"
    rows: list[tuple[str, str, str, str]] = []
    dropped = 0
    for r in zipcodes.list_all():
        st = r.get("state")
        if st not in US_STATES:
            continue
        lat, lon = r.get("lat"), r.get("long")
        if not lat or not lon:
            continue
        flat, flon = float(lat), float(lon)
        # Sanity-bound to the US envelope: a handful of PO-box / unique-entity ZIPs carry a
        # placeholder (0, 0) centroid. Left in, those null-island points would wreck the derived
        # state bboxes, so drop anything outside HI(19)->AK(70.7) lat / AK(-177)->ME(-67) lon.
        if not (17.0 <= flat <= 72.0 and -180.0 <= flon <= -64.0):
            dropped += 1
            continue
        # 4 decimals (~11 m) is plenty for region bboxes and contains() filtering, and keeps the
        # committed file small. ZIP is kept as the source string to preserve leading zeros.
        rows.append((r["zip_code"], st, f"{flat:.4f}", f"{flon:.4f}"))

    rows.sort()
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(("zip", "state", "lat", "lon"))
        w.writerows(rows)
    print(
        f"wrote {len(rows)} ZIPs across {len({r[1] for r in rows})} jurisdictions "
        f"({dropped} out-of-bounds dropped) -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
