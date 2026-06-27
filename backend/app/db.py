import asyncio
import logging

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import settings

log = logging.getLogger("opendrop.db")

pool: AsyncConnectionPool | None = None


async def _wait_for_db(retries: int = 30, delay: float = 2.0) -> None:
    """Belt-and-suspenders against the initdb temp-server race: retry until the DB
    accepts a connection AND the schema is present."""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            conn = await psycopg.AsyncConnection.connect(settings.database_url, connect_timeout=5)
            try:
                await conn.execute("SELECT 1 FROM sources LIMIT 1")
            finally:
                await conn.close()
            log.info("db reachable and schema present")
            return
        except Exception as e:  # noqa: BLE001
            last = e
            log.warning("db not ready (%d/%d): %s", attempt, retries, e)
            await asyncio.sleep(delay)
    raise RuntimeError(f"database never became ready: {last}")


async def open_pool() -> None:
    global pool
    await _wait_for_db()
    pool = AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=10,
        kwargs={"row_factory": dict_row},
        open=False,
    )
    await pool.open(wait=True, timeout=30)
    log.info("db pool open")


async def close_pool() -> None:
    if pool is not None:
        await pool.close()
