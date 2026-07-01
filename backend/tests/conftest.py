import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))             # make `pipeline` importable
sys.path.insert(0, str(ROOT / "backend"))  # make `app` importable

DB_URL = os.environ.get("DATABASE_URL", "postgresql://opendrop:opendrop@localhost:5432/opendrop")


def _db_up() -> bool:
    try:
        import psycopg
        conn = psycopg.connect(DB_URL, connect_timeout=3)
        conn.close()
        return True
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_up(), reason="database not reachable")


@pytest.fixture()
def conn():
    import psycopg
    from psycopg.rows import dict_row
    c = psycopg.connect(DB_URL, row_factory=dict_row)
    yield c
    c.close()


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:  # runs lifespan -> opens db pool
        yield c


# --- test isolation: every DB-backed test starts from a clean slate ----------------------------
# opendrop_test is a PERSISTENT database — nothing truncates it between runs. Without a reset the
# suite is not idempotent: the per-IP-per-day rate-limit caps (corrections, votes, reports, image
# uploads) count rows left behind by PREVIOUS runs, so re-running the suite enough times in a day
# eventually makes a hardcoded-IP correction/vote test return 429 instead of 200 — exactly how
# test_revert_actor_bulk_undoes_every_apply began failing after ~8 runs (corrections_per_ip_per_day
# is 15 and its IP posts 2/run). Wiping the mutable content tables before each test makes every test
# hermetic and the whole suite re-runnable any number of times. The `sources` registry, migration
# bookkeeping and PostGIS metadata are reference data, so they are preserved.
_PRESERVE_TABLES = {"sources", "schema_migrations", "spatial_ref_sys"}

# Truncation ALWAYS runs as the schema owner. The CI restricted-role pass connects the tests as a
# least-privilege role that intentionally lacks TRUNCATE (deploy/app_role.sql), so the reset uses
# TEST_DB_OWNER_URL when provided and otherwise falls back to DATABASE_URL (which is the owner
# everywhere else — the local docker harness and the CI owner pass).
_TRUNCATE_URL = os.environ.get("TEST_DB_OWNER_URL", DB_URL)


@pytest.fixture(autouse=True)
def _isolate_db():
    """Truncate all mutable tables before each DB-backed test, as the owner role.

    Hard safety rail: refuses to touch any database whose name does not end in ``_test`` UNLESS
    OPENDROP_ALLOW_DB_TRUNCATE=1 is set — the explicit opt-in CI uses for its ephemeral, throwaway
    ``opendrop`` service DB. This conftest *defaults* to the live ``opendrop`` DB, so without the
    rail a misconfigured DATABASE_URL could wipe production. No-ops when the DB is unreachable — the
    pure no-DB unit tests neither need nor trigger it."""
    import psycopg

    try:
        c = psycopg.connect(_TRUNCATE_URL, connect_timeout=3)
    except Exception:
        yield  # DB down -> no-DB unit tests run unaffected
        return

    dbname = _TRUNCATE_URL.rsplit("/", 1)[-1].split("?", 1)[0]
    allow_nontest = os.environ.get("OPENDROP_ALLOW_DB_TRUNCATE") == "1"
    if not (dbname.endswith("_test") or allow_nontest):
        c.close()
        raise RuntimeError(
            f"refusing to truncate non-test database {dbname!r}: point DATABASE_URL at an isolated "
            "*_test database, or set OPENDROP_ALLOW_DB_TRUNCATE=1 for an ephemeral throwaway DB")
    try:
        with c.cursor() as cur:
            cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            tables = [r[0] for r in cur.fetchall() if r[0] not in _PRESERVE_TABLES]
            if tables:
                cur.execute(
                    "TRUNCATE " + ", ".join(f'"{t}"' for t in tables) + " RESTART IDENTITY CASCADE")
        c.commit()
    finally:
        c.close()
    yield
