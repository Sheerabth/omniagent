"""Postgres connection pool (psycopg async)."""
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None


async def init_pool() -> None:
    global _pool
    dsn = os.environ["DATABASE_URL"]
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
