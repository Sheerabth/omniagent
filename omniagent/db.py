"""Database connection — SQLAlchemy Core async engine.

ponytail: single engine, pool_size=10, max_overflow=5.
Tune via OMNIAGENT_POOL_SIZE / OMNIAGENT_MAX_OVERFLOW env vars if needed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

_engine = None


async def init_db(dsn: str) -> None:
    """Create the async engine. Called once at startup."""
    global _engine
    _engine = create_async_engine(
        dsn.replace("postgresql://", "postgresql+psycopg://"),
        pool_size=10,
        max_overflow=5,
        isolation_level="AUTOCOMMIT",
    )


async def close_db() -> None:
    """Dispose the engine. Called at shutdown."""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None


@asynccontextmanager
async def get_conn() -> AsyncIterator[AsyncConnection]:
    """Yield a SQLAlchemy AsyncConnection from the engine pool."""
    if _engine is None:
        raise RuntimeError("Database not initialised — call init_db first")
    async with _engine.connect() as conn:
        yield conn
