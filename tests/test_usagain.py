"""USAgain scraper tests — green clothing-donation bins (no DB, no network).

usagain.py GETs a server-rendered results page per ZIP. The page carries Google-Maps marker
blocks (`new google.maps.LatLng(lat, lon)`) AND a results `<table class="table">`. The module-level
pure `_parse(html)`:

  * pulls every LatLng via `_LATLNG`; the FIRST is the search-center marker and is DROPPED, the rest
    are bins;
  * reads `<table.table> <tr>` rows, keeping each row's first two `<td>` cells as (name, address),
    paired to bins BY INDEX;
  * falls back to "USAgain donation bin" when a name cell is empty/absent.

`fetch()` sweeps `region.zips`, dedupes bins on coords rounded to 5 dp, drops anything outside
`region.contains(..., margin=0.05)`, and bumps `fetch_failures` when a ZIP GET raises.

These tests exercise `_parse` directly with hand-built HTML that DEMONSTRABLY matches `_LATLNG`
(verified: flexible whitespace, two `[-\\d.]+` groups) and the selectolax selector (verified: a
`<th>`-only header row yields zero `<td>` cells and is skipped; `<td>` rows are collected in
document order). `fetch()` is driven through a faked context-manager PoliteClient.
Run: PYTHONPATH=. pytest tests/test_usagain.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from pipeline.regions import Region  # noqa: E402
from pipeline.scrapers import usagain  # noqa: E402


# --- HTML fixture builders (every piece is verified against the real regex/selectors) ------------

def _marker(lat, lon):
    """A Google-Maps marker block exactly as `_LATLNG` matches it."""
    return f"var m = new google.maps.LatLng({lat}, {lon});"


def _row(name, address):
    return f"<tr><td>{name}</td><td>{address}</td></tr>"


def _page(coords, rows, with_header=True):
    """Assemble a results page: one marker per coord + a `table.table` of (name, address) rows.

    `coords[0]` is the search center (dropped by `_parse`); `coords[1:]` are bins. A `<th>`-only
    header row is included by default to prove it is skipped (it contributes no `<td>` cells).
    """
    markers = "\n".join(_marker(lat, lon) for lat, lon in coords)
    header = "<tr><th>Name</th><th>Address</th></tr>" if with_header else ""
    body = "\n".join(_row(n, a) for n, a in rows)
    return f"""<html><body>
<script>
{markers}
</script>
<table class="table">
<thead>{header}</thead>
<tbody>
{body}
</tbody>
</table>
</body></html>"""


# --- _parse(): the pure parser -------------------------------------------------------------------

def test_parse_multi_bin_drops_center_and_pairs_rows_by_index():
    # coords[0] = search center (DROPPED); two bins follow, paired to two table rows by index.
    center = (40.00000, -83.00000)
    bin1 = (40.10000, -83.10000)
    bin2 = (40.20000, -83.20000)
    html = _page(
        [center, bin1, bin2],
        [("Bin Alpha", "1 Main St"), ("Bin Beta", "2 Oak Ave")],
    )

    recs = list(usagain._parse(html))

    assert len(recs) == 2  # exactly the two bins; the center marker is not a bin
    r1, r2 = recs
    # Coords come from the LatLng markers (center skipped), in order.
    assert (r1.lat, r1.lon) == bin1
    assert (r2.lat, r2.lon) == bin2
    # name/address pair to the table rows by index.
    assert r1.name == "Bin Alpha" and r1.address_line == "1 Main St"
    assert r2.name == "Bin Beta" and r2.address_line == "2 Oak Ave"
    # source_ref is the 5-dp formatted coordinate; constant fields are fixed by the source.
    assert r1.source_ref == f"{bin1[0]:.5f},{bin1[1]:.5f}"
    assert r2.source_ref == "40.20000,-83.20000"
    for r in recs:
        assert r.org_type == "drop_bin"
        assert r.org_name == "USAgain"
        assert r.accepted_items == ["clothing", "shoes"]
        assert r.hours == {"always": True}


def test_parse_only_center_marker_yields_no_bins():
    # A single LatLng (just the search center) => len(coords) <= 1 => `_parse` returns nothing.
    html = _page([(40.0, -83.0)], rows=[])
    assert list(usagain._parse(html)) == []


def test_parse_no_latlng_yields_nothing():
    # No marker blocks at all (regex finds zero coords) => nothing, even with a populated table.
    html = """<html><body>
<table class="table"><tbody>
<tr><td>Orphan Bin</td><td>9 Nowhere Rd</td></tr>
</tbody></table>
</body></html>"""
    assert list(usagain._parse(html)) == []


def test_parse_more_bins_than_rows_falls_back_to_default_name():
    # Two bins but the table only has one row -> the second bin has no row, so name falls back to the
    # source default and address_line is None (the `if i < len(rows)` guard leaves them None).
    center = (40.0, -83.0)
    bin1 = (40.10000, -83.10000)
    bin2 = (40.20000, -83.20000)
    html = _page([center, bin1, bin2], [("Only Row", "123 Single St")])

    recs = list(usagain._parse(html))

    assert len(recs) == 2
    assert recs[0].name == "Only Row" and recs[0].address_line == "123 Single St"
    assert recs[1].name == "USAgain donation bin"  # default fallback (no matching row)
    assert recs[1].address_line is None


def test_parse_empty_name_cell_falls_back_but_keeps_address():
    # A row whose first cell is empty: `rows[i][0] or None` -> None -> name falls back to default,
    # while the non-empty address cell is preserved.
    center = (40.0, -83.0)
    bin1 = (40.11111, -83.11111)
    html = _page([center, bin1], [("", "55 Elm St")])

    (rec,) = list(usagain._parse(html))

    assert rec.name == "USAgain donation bin"  # empty name -> default
    assert rec.address_line == "55 Elm St"


def test_parse_header_row_is_skipped_not_consumed_as_a_bin():
    # The `<th>`-only header contributes no `<td>` cells, so it must NOT shift the row/bin pairing.
    center = (40.0, -83.0)
    bin1 = (40.10000, -83.10000)
    html = _page([center, bin1], [("Real Bin", "7 First Ave")], with_header=True)

    (rec,) = list(usagain._parse(html))

    # If the header row had been counted, rows[0] would be empty and name would default. It doesn't.
    assert rec.name == "Real Bin" and rec.address_line == "7 First Ave"


# --- fetch(): the ZIP sweep over a faked client --------------------------------------------------

class _FakeResp:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    """Context-manager client mapping each ZIP param to canned HTML; a sentinel raises (failed GET).

    `pages` is {zip -> html | RAISE}. Mirrors usagain.fetch's call: client.get(URL, params={"zip": z}).
    """
    RAISE = object()

    def __init__(self, pages):
        self._pages = pages

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None):
        page = self._pages[params["zip"]]
        if page is self.RAISE:
            raise RuntimeError("boom: ZIP fetch failed")
        return _FakeResp(page)


def _region(zips):
    # Compact region around (40.0, -83.0). contains() uses a 0.05 deg margin in fetch().
    return Region("ua_t", (39.90, -83.10, 40.10, -82.90), (40.0, -83.0), zips, 150)


def _install(monkeypatch, pages):
    monkeypatch.setattr(usagain, "PoliteClient", lambda *a, **k: _FakeClient(pages))


def test_fetch_dedupes_repeated_coords_across_zips(monkeypatch):
    # Same bin (identical coords to 5 dp) appears under two ZIPs -> yielded once.
    center = (40.0, -83.0)
    bin_shared = (40.00000, -82.95000)  # inside the bbox
    page = _page([center, bin_shared], [("Shared Bin", "1 Shared St")])
    _install(monkeypatch, {"43004": page, "43017": page})

    recs = list(usagain.UsAgainScraper().fetch(_region(["43004", "43017"])))

    refs = [r.source_ref for r in recs]
    assert refs == ["40.00000,-82.95000"]  # deduped to a single record
    assert len(recs) == 1


def test_fetch_dedupes_on_5dp_rounding(monkeypatch):
    # Two coords that differ only past the 5th decimal collapse to the same rounded dedupe key.
    center = (40.0, -83.0)
    a = (40.000001, -82.950001)   # rounds to 40.00000, -82.95000
    b = (40.000004, -82.950004)   # same 5-dp key
    page = _page([center, a, b], [("A", "1 St"), ("B", "2 St")])
    _install(monkeypatch, {"43004": page})

    recs = list(usagain.UsAgainScraper().fetch(_region(["43004"])))

    assert len(recs) == 1  # second bin collapses onto the first's rounded key


def test_fetch_drops_out_of_region_bins(monkeypatch):
    # One bin inside the bbox, one within the 0.05 margin (kept), one far away (dropped).
    center = (40.0, -83.0)
    inside = (40.00000, -83.00000)   # squarely inside
    border = (40.13000, -83.00000)   # 0.03 past the north edge -> within the 0.05 margin (kept)
    far = (41.50000, -83.00000)      # ~1.4 deg north -> dropped
    page = _page(
        [center, inside, border, far],
        [("In", "1 In St"), ("Border", "2 Border St"), ("Far", "3 Far St")],
    )
    _install(monkeypatch, {"43004": page})

    refs = {r.source_ref for r in usagain.UsAgainScraper().fetch(_region(["43004"]))}

    assert "40.00000,-83.00000" in refs   # inside kept
    assert "40.13000,-83.00000" in refs   # border within margin kept
    assert "41.50000,-83.00000" not in refs  # far bin dropped


def test_fetch_counts_fetch_failures_on_failed_zip_get(monkeypatch):
    # A raising ZIP GET is swallowed: fetch_failures bumps and that ZIP contributes no records,
    # while a healthy ZIP still yields its bin.
    center = (40.0, -83.0)
    good_bin = (40.01000, -82.99000)
    good = _page([center, good_bin], [("Good", "1 Good St")])
    _install(monkeypatch, {"43004": good, "43017": _FakeClient.RAISE})

    scraper = usagain.UsAgainScraper()
    recs = list(scraper.fetch(_region(["43004", "43017"])))

    assert scraper.fetch_failures == 1  # exactly one ZIP raised
    assert {r.source_ref for r in recs} == {"40.01000,-82.99000"}  # only the healthy ZIP's bin


def test_fetch_empty_zips_yields_nothing_and_no_failures(monkeypatch):
    # No ZIPs -> the sweep loop never runs: no records, no fetch_failures, no client calls needed.
    _install(monkeypatch, {})
    scraper = usagain.UsAgainScraper()
    assert list(scraper.fetch(_region([]))) == []
    assert scraper.fetch_failures == 0


# --- Property-based invariants -------------------------------------------------------------------

_LAT = st.floats(min_value=39.91, max_value=40.09)   # strictly inside the test region bbox
_LON = st.floats(min_value=-83.09, max_value=-82.91)


@settings(max_examples=120)
@given(
    bins=st.lists(st.tuples(_LAT, _LON), min_size=1, max_size=6),
)
def test_parse_invariant_one_record_per_bin_marker(bins):
    """INVARIANT: with N+1 LatLng markers (1 center + N bins) and a matching row per bin, `_parse`
    yields exactly N records, in marker order, each tagged as a USAgain drop bin."""
    center = (40.0, -83.0)
    rows = [(f"Bin {i}", f"{i} Some St") for i in range(len(bins))]
    html = _page([center] + list(bins), rows)

    recs = list(usagain._parse(html))

    assert len(recs) == len(bins)  # center dropped, one record per bin marker
    for rec, (lat, lon) in zip(recs, bins):
        # Coordinates round-trip through the 5-dp source_ref formatting.
        assert rec.source_ref == f"{lat:.5f},{lon:.5f}"
        assert rec.org_type == "drop_bin" and rec.org_name == "USAgain"


@settings(max_examples=120)
@given(coords=st.lists(st.tuples(_LAT, _LON), min_size=1, max_size=8))
def test_fetch_invariant_output_is_deduped_and_in_region(coords):
    """INVARIANT: every record `fetch` yields is inside the region (with margin) and the yielded
    dedupe keys are unique — regardless of how many duplicate/over-margin markers the page carries.

    Patches `usagain.PoliteClient` manually (try/finally) rather than via the function-scoped
    monkeypatch fixture, which Hypothesis rejects because it is not reset between generated inputs.
    """
    center = (40.0, -83.0)
    rows = [(f"B{i}", f"{i} Rd") for i in range(len(coords))]
    page = _page([center] + list(coords), rows)
    region = _region(["43004"])

    real = usagain.PoliteClient
    usagain.PoliteClient = lambda *a, **k: _FakeClient({"43004": page})
    try:
        recs = list(usagain.UsAgainScraper().fetch(region))
    finally:
        usagain.PoliteClient = real

    keys = [(round(r.lat, 5), round(r.lon, 5)) for r in recs]
    assert len(keys) == len(set(keys))  # no duplicate keys survive
    for r in recs:
        assert region.contains(r.lat, r.lon, margin=0.05)  # all in-region


if __name__ == "__main__":
    print("Run with: PYTHONPATH=. pytest tests/test_usagain.py")
