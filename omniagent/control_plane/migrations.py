"""Auto-apply SQL migrations from migrations/ directory on startup."""

import logging
import os

import psycopg

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "migrations")


async def run_migrations(dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """
        )

        rows = await conn.execute("SELECT filename FROM schema_migrations")
        applied = {r[0] for r in await rows.fetchall()}

        files = sorted(f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql"))
        pending = [f for f in files if f not in applied]
        logger.info("Migrations: %d applied, %d pending", len(applied), len(pending))
        for filename in pending:
            path = os.path.join(MIGRATIONS_DIR, filename)
            with open(path) as f:
                sql = f.read()
            logger.info("Applying migration: %s", filename)
            await conn.execute(sql)
            await conn.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (filename,))
            logger.info("Applied: %s", filename)
        if not pending:
            logger.info("All migrations up to date")
