"""Postgres connection pool (psycopg async)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def init_pool() -> None:
    global _pool
    from omniagent.config import settings

    dsn = settings.database_url
    _pool = AsyncConnectionPool(dsn, min_size=2, max_size=10, open=False)
    await _pool.open()


async def close_pool() -> None:
    if _pool:
        await _pool.close()


@asynccontextmanager
async def get_conn() -> AsyncIterator[psycopg.AsyncConnection]:
    assert _pool is not None, "DB pool not initialised"
    async with _pool.connection() as conn:
        conn.row_factory = dict_row
        yield conn
