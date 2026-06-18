"""FastAPI control plane."""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from omniagent.control_plane import db, queue
from omniagent.control_plane.routes import agents, internal, sessions, settings, skills, sse, tools

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_UI_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "ui")


async def _seed_builtin_ui_key() -> str:
    """Ensure a fixed API key exists for the built-in UI. Uses OMNIAGENT_API_KEY
    from env (stable across restarts). Upserts so the table doesn't grow."""
    from omniagent.control_plane.secrets import generate_key, hash_key

    api_key = os.environ.get("OMNIAGENT_API_KEY") or generate_key()
    key_hash = hash_key(api_key)
    key_prefix = api_key[:8]

    async with db.get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO api_keys (name, key_hash, key_prefix)
            VALUES ('_built-in-ui', %s, %s)
            ON CONFLICT (name) DO UPDATE
              SET key_hash    = EXCLUDED.key_hash,
                  key_prefix  = EXCLUDED.key_prefix
            """,
            (key_hash, key_prefix),
        )
    return api_key


async def _mark_session_failed(session_id: str) -> None:
    import uuid

    sid = uuid.UUID(session_id)
    async with db.get_conn() as conn:
        await conn.execute(
            "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s AND status='running'",
            (sid,),
        )
    await internal._notify(sid, "error")


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
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from omniagent.control_plane.migrations import run_migrations
    from omniagent.worker.job import app as proc_app

    dsn = os.environ.get("DATABASE_URL", "")
    await run_migrations(dsn)
    await db.init_pool()
    app.state.ui_api_key = await _seed_builtin_ui_key()
    await _reconcile_stuck_sessions()
    queue.set_session_fail_callback(_mark_session_failed)

    async with proc_app.open_async():
        yield

    await db.close_pool()


app = FastAPI(title="OmniAgent Control Plane", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def ui(request: Request) -> HTMLResponse:
    path = os.path.join(_UI_DIR, "index.html")
    with open(path) as f:
        html = f.read()
    key_meta = f'<meta name="omniagent-api-key" content="{request.app.state.ui_api_key}">'
    html = html.replace('<meta charset="UTF-8">', f'<meta charset="UTF-8">\n{key_meta}')
    return HTMLResponse(html)


app.include_router(tools.router)
app.include_router(skills.router)
app.include_router(agents.router)
app.include_router(sessions.router)
app.include_router(settings.router)
app.include_router(internal.router)
app.include_router(sse.router)
