"""Auto-apply SQL migrations from migrations/ directory on startup."""

import logging
import os

import procrastinate
import psycopg
from procrastinate import PsycopgConnector

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "migrations")


async def run_migrations(dsn: str) -> None:
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
        # Procrastinate schema — locked to prevent concurrent apply on first boot
        await conn.execute("SELECT pg_advisory_lock(hashtext('omniagent_procrastinate_schema'))")
        try:
            row = await conn.execute(
                "SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'procrastinate_jobs'"
            )
            if not await row.fetchone():
                proc_app = procrastinate.App(connector=PsycopgConnector(conninfo=dsn))
                async with proc_app.open_async():
                    await proc_app.schema_manager.apply_schema_async()
                logger.info("Procrastinate schema applied")
        finally:
            await conn.execute(
                "SELECT pg_advisory_unlock(hashtext('omniagent_procrastinate_schema'))"
            )

        # Custom migrations
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
            # Per-migration advisory lock — only one instance runs each migration
            lock_id = f"omniagent_migration_{filename}"
            await conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock_id,))
            try:
                # Re-check after acquiring lock — another instance may have applied it
                row = await conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE filename = %s", (filename,)
                )
                if await row.fetchone():
                    logger.info("Migration already applied (concurrent): %s", filename)
                    continue
                path = os.path.join(MIGRATIONS_DIR, filename)
                with open(path) as f:
                    sql = f.read()
                logger.info("Applying migration: %s", filename)
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING",
                        (filename,),
                    )
                logger.info("Applied: %s", filename)
            finally:
                await conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_id,))
