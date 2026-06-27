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
