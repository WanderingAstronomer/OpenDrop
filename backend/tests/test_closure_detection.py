"""Closure-detection safety — the regression suite for the ~633-bin erosion (migration 0011).

The production failure: closure/deletion detection retired ~633 REAL donation bins. Two blind spots:
  * A *clean* run of a NON-exhaustive source (Planet Aid nearest-20 grid, ZIP-radius sweeps)
    legitimately omits real bins it never sampled — yet reconcile deleted their links as "closed".
  * The single-run 40% breaker is blind to sub-40% bleed that accumulates across many runs
    (Planet Aid eroded 643 links across 53 regional seed runs, each individually under threshold).

The fix is three layers in `pipeline/scrapers/base._reconcile`:
  0. exhaustive gate   — only sources.fetch_is_exhaustive may reconcile at all (OSM only).
  1. single-run breaker — a truncated/blocked response can't mass-retire in one run.
  2. cumulative breaker — slow cross-run bleed trips a breaker via the reconcile_audit ledger.
Plus a per-run completeness signal (`scraper.fetch_failures`) so even an exhaustive source never
reconciles a region whose fetch was incomplete (a dead Overpass tile).

Every test isolates its data in an empty US bbox with CLT- source refs and cleans up after.
(`opendrop_test` carries no seeded bins, so in-region counts reflect only what each test inserts.)
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from conftest import requires_db  # noqa: E402
from pipeline import store  # noqa: E402
from pipeline.regions import Region  # noqa: E402
from pipeline.scrapers import base  # noqa: E402

# Every test here drives the pipeline reconcile/closure path (INSERT+UPDATE on location_sources,
# DELETE on the source links) — work that runs as the schema OWNER in prod, never as the restricted
# app role. Mark owner_only so the CI `-m "not owner_only"` restricted pass deselects them instead
# of erroring on "permission denied".
pytestmark = [requires_db, pytest.mark.owner_only]

# Empty Montana-wilderness bbox: inside the _US envelope (so load()'s _in_us gate accepts the fake
# records) but with no real donation bins to collide with.
_BBOX = (45.00, -110.00, 45.40, -109.60)  # (south, west, north, east)
_REF_PREFIX = "CLT-"


def _coords(i):
    return (45.05 + i * 0.002, -109.95 + i * 0.002)  # stays inside _BBOX for i < ~170


def _region(name):
    return Region(name, _BBOX, (45.20, -109.80), [], 25)


def _seed(conn, code, n, tag):
    """Insert n locations + one `code` link each, spread inside _BBOX. Returns [(ref, lat, lon)]."""
    out = []
    for i in range(n):
        lat, lon = _coords(i)
        lid = conn.execute(
            "INSERT INTO locations (geom,name,org_type) "
            "VALUES (ST_SetSRID(ST_MakePoint(%s,%s),4326),%s,'drop_bin') RETURNING id",
            (lon, lat, f"CLT bin {tag}{i}"),
        ).fetchone()["id"]
        ref = f"{_REF_PREFIX}{tag}{i}"
        conn.execute(
            "INSERT INTO location_sources (location_id,source_code,source_ref,source_geom) "
            "VALUES (%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326))",
            (lid, code, ref, lon, lat),
        )
        out.append((ref, lat, lon))
    conn.commit()
    return out


def _bbox_link_count(conn, code):
    s, w, n, e = _BBOX
    return conn.execute(
        "SELECT count(*) AS n FROM location_sources WHERE source_code=%s "
        "AND source_geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)",
        (code, w, s, e, n),
    ).fetchone()["n"]


@pytest.fixture()
def clean(conn):
    """Clear the test bbox (for both sources we touch) + any prior audit rows before and after."""
    def _wipe():
        s, w, n, e = _BBOX
        for code in ("planet_aid", "osm"):
            conn.execute(
                "DELETE FROM location_sources WHERE source_code=%s "
                "AND source_geom && ST_MakeEnvelope(%s,%s,%s,%s,4326)",
                (code, w, s, e, n),
            )
        conn.execute("DELETE FROM locations WHERE name LIKE 'CLT bin %'")
        conn.execute("DELETE FROM reconcile_audit WHERE region_key LIKE 'cltest_%'")
        conn.commit()
    _wipe()
    yield
    _wipe()


# --- Layer 0: the exhaustive gate (the primary fix for the 633) ------------------------------

def test_nonexhaustive_clean_run_never_retires(conn, clean):
    """A non-exhaustive source (planet_aid) whose clean run omits 1 of 10 real bins must NOT delete
    it — even though the omission is under the single-run 40% threshold. This is the exact bug."""
    recs = _seed(conn, "planet_aid", 10, "ne")
    src = store.get_source(conn, "planet_aid")
    assert src["fetch_is_exhaustive"] is False  # the data condition that makes the gate fire

    seen = {r[0] for r in recs[:9]}  # the grid happened to miss recs[9] — a real bin still out there
    removed = base._reconcile(conn, src, _region("cltest_ne"), seen)

    assert removed == 0
    assert _bbox_link_count(conn, "planet_aid") == 10  # nothing retired
    survivor = conn.execute(
        "SELECT status, source_count FROM locations WHERE name=%s", ("CLT bin ne9",)
    ).fetchone()
    assert survivor["status"] == "active" and survivor["source_count"] == 1


def test_override_proves_scenario_reaches_deletion(conn, clean, monkeypatch):
    """RED-catching proof: the SAME scenario, with the exhaustive gate disabled (the operator
    escape hatch), DOES delete the missed bin. So the test above is real — the gate is what saves
    the bin, not an artifact of the scenario never reaching the delete path."""
    recs = _seed(conn, "planet_aid", 10, "ov")
    src = store.get_source(conn, "planet_aid")
    monkeypatch.setattr(base, "_RECONCILE_IGNORE_EXHAUSTIVE", True)

    removed = base._reconcile(conn, src, _region("cltest_ov"), {r[0] for r in recs[:9]})

    assert removed == 1  # without the gate, the clean-but-incomplete run retires the real bin
    assert _bbox_link_count(conn, "planet_aid") == 9


# --- Layer 0 (positive): an exhaustive source SHOULD reconcile a genuine closure ---------------

def test_exhaustive_source_retires_genuine_closure(conn, clean):
    """OSM is exhaustive (Overpass enumerates every node in the bbox), so a node truly absent from a
    complete sweep IS closed and should be retired — and an executed audit row is written."""
    recs = _seed(conn, "osm", 10, "ex")
    src = store.get_source(conn, "osm")
    assert src["fetch_is_exhaustive"] is True

    removed = base._reconcile(conn, src, _region("cltest_ex"), {r[0] for r in recs[:9]})  # ex9 gone

    assert removed == 1
    assert _bbox_link_count(conn, "osm") == 9
    audit = conn.execute(
        "SELECT executed, reason, retired, baseline FROM reconcile_audit "
        "WHERE region_key=%s ORDER BY id DESC LIMIT 1", ("cltest_ex",),
    ).fetchone()
    assert audit["executed"] is True and audit["reason"] is None
    assert audit["retired"] == 1 and audit["baseline"] == 10


# --- Layer 1: single-run breaker --------------------------------------------------------------

def test_single_run_breaker_blocks_mass_retire(conn, clean):
    """Even for an exhaustive source, a run that would retire > 40% in one shot (a truncated/blocked
    upstream response) is refused and logged to the audit ledger."""
    recs = _seed(conn, "osm", 10, "sr")
    src = store.get_source(conn, "osm")

    removed = base._reconcile(conn, src, _region("cltest_sr"), {r[0] for r in recs[:5]})  # 5/10 = 50%

    assert removed == 0
    assert _bbox_link_count(conn, "osm") == 10
    audit = conn.execute(
        "SELECT executed, reason FROM reconcile_audit WHERE region_key=%s ORDER BY id DESC LIMIT 1",
        ("cltest_sr",),
    ).fetchone()
    assert audit["executed"] is False and audit["reason"] == "single_run_breaker"


def test_min_seen_breaker_blocks_tiny_run(conn, clean):
    """A run that saw fewer than _RECONCILE_MIN_SEEN records can't be trusted to reconcile anything."""
    recs = _seed(conn, "osm", 10, "ms")
    src = store.get_source(conn, "osm")

    removed = base._reconcile(conn, src, _region("cltest_ms"), {r[0] for r in recs[:2]})  # seen=2 < 5

    assert removed == 0
    assert _bbox_link_count(conn, "osm") == 10


# --- Layer 2: cumulative cross-run erosion breaker --------------------------------------------

def test_cumulative_breaker_trips_on_slow_bleed(conn, clean):
    """A single run that passes the single-run breaker is still refused if prior retirements in the
    window already approached the cumulative ceiling — the slow-bleed mode that erased the 633."""
    region_key = "cltest_cum"
    # Prior history for this source+region: the region once held 100 links and 40 have already been
    # retired in the window (each prior run individually under the single-run breaker).
    conn.execute(
        "INSERT INTO reconcile_audit (source_code,region_key,baseline,would_retire,retired,"
        "seen_count,executed,reason) VALUES ('osm',%s,100,40,40,60,true,NULL)",
        (region_key,),
    )
    conn.commit()

    recs = _seed(conn, "osm", 60, "cum")  # 60 links remain now (100 peak - 40 retired)
    src = store.get_source(conn, "osm")
    # This run would retire just 1 (well under single-run 40%), but 40 prior + 1 = 41 > 40% of the
    # high-water-mark 100 -> cumulative breaker must trip.
    removed = base._reconcile(conn, src, _region(region_key), {r[0] for r in recs[:59]})

    assert removed == 0
    assert _bbox_link_count(conn, "osm") == 60
    audit = conn.execute(
        "SELECT executed, reason FROM reconcile_audit WHERE region_key=%s ORDER BY id DESC LIMIT 1",
        (region_key,),
    ).fetchone()
    assert audit["executed"] is False and audit["reason"] == "cumulative_breaker"


# --- Per-run completeness gate (load level): incomplete fetch never reconciles -----------------

class _FakeOsm(base.BaseScraper):
    """An exhaustive source (reuses the registered 'osm' code) whose fetch reports a swallowed
    failure — so `load` must refuse to reconcile even though the source is exhaustive."""
    code = "osm"

    def __init__(self, records, failures):
        self._records = records      # [(ref, lat, lon)] the run re-saw
        self._failures = failures

    def fetch(self, region):
        self.fetch_failures += self._failures  # simulate dead tile(s)
        for ref, lat, lon in self._records:
            yield base.NormalizedRecord(source_ref=ref, name=f"CLT bin {ref}",
                                        org_type="drop_bin", lat=lat, lon=lon)


def test_incomplete_fetch_blocks_reconcile_in_load(conn, clean):
    """`load` gates reconcile on a provably complete fetch: fetch_failures>0 means `seen` is
    incomplete, so an absent link is not provably closed and must not be retired."""
    recs = _seed(conn, "osm", 10, "if")
    # The scraper re-sees 9 of the 10 (would retire 1 — under every fraction breaker) BUT reports a
    # dead tile. Without the completeness gate, OSM being exhaustive would retire the 10th.
    scraper = _FakeOsm(records=recs[:9], failures=1)
    base.load(scraper, _region("cltest_if"), conn)

    assert _bbox_link_count(conn, "osm") == 10  # the unseen link survived: fetch was not complete


def test_complete_fetch_does_reconcile_in_load(conn, clean):
    """Contrast to the above: the SAME load path with a COMPLETE fetch (fetch_failures=0) does
    retire the unseen link — proving the survival above is the completeness gate, not inertia."""
    recs = _seed(conn, "osm", 10, "cf")
    scraper = _FakeOsm(records=recs[:9], failures=0)
    base.load(scraper, _region("cltest_cf"), conn)

    assert _bbox_link_count(conn, "osm") == 9  # complete sweep -> the genuinely-absent link retired
