"""Postgres connection pool (psycopg async)."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any, cast

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

_pool: AsyncConnectionPool | None = None

# DictRow connection — conn.row_factory = dict_row is set in get_conn() below.
# psycopg's type stubs default to AsyncConnection[TupleRow], but dict_row makes
# every row a dict[str, Any]. Cast at yield so pyright treats fetchone/fetchall
# results as DictRow throughout the codebase.
DictConn = psycopg.AsyncConnection[dict[str, Any]]


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
async def get_conn() -> AsyncGenerator[DictConn, None]:
    assert _pool is not None, "DB pool not initialised"
    async with _pool.connection() as conn:
        conn.row_factory = dict_row  # pyright: ignore[reportAttributeAccessIssue]
        yield cast(DictConn, conn)
