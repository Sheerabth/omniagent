"""FastAPI control plane."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse

from omniagent.control_plane import db, queue, redis_client
from omniagent.control_plane.routes import agents, internal, sessions, settings, skills, sse, tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_UI_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "ui")


async def _mark_session_failed(session_id: str) -> None:
    import uuid

    sid = uuid.UUID(session_id)
    async with db.get_conn() as conn:
        await conn.execute(
            "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s AND status='running'",
            (sid,),
        )
    await internal._publish(sid, {"type": "error", "reason": "job failed or timed out"})


async def _reconcile_stuck_sessions() -> None:
    async with db.get_conn() as conn:
        # Advisory lock prevents race when multiple CP instances start simultaneously
        locked = await conn.execute("SELECT pg_try_advisory_lock(hashtext('omniagent_reconcile'))")
        if not (await locked.fetchone())["pg_try_advisory_lock"]:
            logger.info("reconcile: another instance holds lock, skipping")
            return
        try:
            rows = await conn.execute("SELECT id FROM sessions WHERE status = 'running'")
            stuck = await rows.fetchall()
            for row in stuck:
                logger.warning("reconcile: marking stuck session %s as failed", row["id"])
                await conn.execute(
                    "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s",
                    (row["id"],),
                )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext('omniagent_reconcile'))")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from omniagent.control_plane.migrations import run_migrations
    from omniagent.worker.job import app as proc_app

    dsn = os.environ.get("DATABASE_URL", "")
    await run_migrations(dsn)
    await db.init_pool()
    await redis_client.init_redis()
    await _reconcile_stuck_sessions()
    queue.set_session_fail_callback(_mark_session_failed)

    async with proc_app.open_async():
        yield

    await redis_client.close_redis()
    await db.close_pool()


app = FastAPI(title="OmniAgent Control Plane", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def ui():
    return FileResponse(os.path.join(_UI_DIR, "index.html"))


app.include_router(tools.router)
app.include_router(skills.router)
app.include_router(agents.router)
app.include_router(sessions.router)
app.include_router(settings.router)
app.include_router(internal.router)
app.include_router(sse.router)
