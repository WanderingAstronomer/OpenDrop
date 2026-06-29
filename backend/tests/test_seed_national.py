"""National seeder resumability (DB-backed, migration 0008's seed_progress table).

pipeline/seed_national.py is built to run for hours and survive interruption, so its checkpoint
orchestration is what's pinned here: seed every state once, run the global finalize exactly once,
skip already-`done` states on resume, and re-run everything under SEED_FORCE. The scrapers,
dedup, and promote are stubbed — this is about control flow, not the network or PostGIS.

Each test owns synthetic `zz_*` region rows (plus the shared `__finalize__` row) and clears them,
so it never touches a real seed and never collides with other tests.
"""
import psycopg
import pytest
from psycopg.rows import dict_row

from conftest import DB_URL, requires_db

import pipeline.seed_national as sn
from pipeline.regions import Region

# The seeder is a pipeline job that runs as the schema-owner role in prod (bulk insert + checkpoint
# writes); it is not part of the least-privilege API surface, so it's excluded from the app-role CI pass.
pytestmark = pytest.mark.owner_only


class _FakeScraper:
    def __init__(self, code):
        self.code = code


def _regions():
    bbox, center = (1.0, 2.0, 3.0, 4.0), (2.0, 3.0)
    return [Region("zz_a", bbox, center, ["1"], 150),
            Region("zz_b", bbox, center, ["2"], 150)]


def _patch(monkeypatch, calls):
    """Stub the seeder's I/O: record (state, scraper) load calls; no-op dedup/promote; give main()
    its own DB connection to the test DB."""
    def fake_load(scraper, region, conn):
        calls.append((region.name, scraper.code))
        return {"upserted": 1}

    monkeypatch.setattr(sn, "state_regions", _regions)
    monkeypatch.setattr(sn, "_scrapers", lambda: [_FakeScraper("osm"), _FakeScraper("sa")])
    monkeypatch.setattr(sn, "load", fake_load)
    monkeypatch.setattr(sn.dedup, "run", lambda conn: 0)
    monkeypatch.setattr(sn.promote, "run", lambda conn: 0)
    monkeypatch.setattr(sn.db, "connect", lambda: psycopg.connect(DB_URL, row_factory=dict_row))


def _statuses(conn):
    conn.rollback()  # fresh snapshot of whatever the seeder's own connection committed
    return {r["region_name"]: r["status"]
            for r in conn.execute("SELECT region_name, status FROM seed_progress").fetchall()}


def _clear(conn):
    conn.execute("DELETE FROM seed_progress WHERE region_name LIKE 'zz_%%' OR region_name = %s",
                 (sn._FINALIZE,))
    conn.commit()


@requires_db
def test_first_run_seeds_all_states_then_finalizes(monkeypatch, conn):
    _clear(conn)
    calls: list = []
    _patch(monkeypatch, calls)
    sn.main()
    st = _statuses(conn)
    assert st.get("zz_a") == "done" and st.get("zz_b") == "done"
    assert st.get(sn._FINALIZE) == "done"
    assert len(calls) == 4  # 2 states x 2 scrapers
    _clear(conn)


@requires_db
def test_resume_skips_completed_states_and_finalize(monkeypatch, conn):
    _clear(conn)
    calls: list = []
    _patch(monkeypatch, calls)
    sn.main()                       # first full pass
    assert len(calls) == 4
    calls.clear()
    sn.main()                       # resume: every state already 'done'
    assert calls == []              # nothing re-swept
    assert _statuses(conn).get(sn._FINALIZE) == "done"
    _clear(conn)


@requires_db
def test_interrupted_state_is_rerun_on_resume(monkeypatch, conn):
    _clear(conn)
    calls: list = []
    _patch(monkeypatch, calls)
    # Simulate a crash mid-zz_b: zz_a done, zz_b left 'running' (never reached 'done').
    c = psycopg.connect(DB_URL, row_factory=dict_row)
    try:
        sn._ensure_progress_table(c)
        sn._mark(c, "zz_a", "done", finish=True)
        sn._mark(c, "zz_b", "running", start=True)
    finally:
        c.close()
    sn.main()
    # zz_a skipped (already done), zz_b re-run (was only 'running'), then finalize.
    assert ("zz_a", "osm") not in calls
    assert ("zz_b", "osm") in calls and ("zz_b", "sa") in calls
    assert _statuses(conn).get("zz_b") == "done"
    _clear(conn)


@requires_db
def test_seed_force_ignores_checkpoints(monkeypatch, conn):
    _clear(conn)
    calls: list = []
    _patch(monkeypatch, calls)
    sn.main()
    assert len(calls) == 4
    calls.clear()
    monkeypatch.setenv("SEED_FORCE", "1")
    sn.main()                       # forced: re-run every state despite 'done'
    assert len(calls) == 4
    _clear(conn)
