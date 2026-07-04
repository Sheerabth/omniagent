"""FastAPI control plane."""

import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from omniagent.api import db, queue
from omniagent.api.metrics import REQUEST_COUNT, REQUEST_LATENCY, render_metrics
from omniagent.api.routes import (
    agents,
    auth,
    memory,
    namespaces,
    oauth2,
    schedules,
    sessions,
    settings,
    sse,
    toolboxes,
    tools,
)
from omniagent.logging_config import configure_logging, trace_id_var

configure_logging()
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
    ch = "session_" + str(sid).replace("-", "_")
    async with db.get_conn() as conn:
        await conn.execute("SELECT pg_notify(%s, %s)", (ch, "error"))


async def _reconcile_stuck_sessions() -> None:
    async with db.get_conn() as conn:
        # Advisory lock prevents race when multiple CP instances start simultaneously
        locked = await conn.execute("SELECT pg_try_advisory_lock(hashtext('omniagent_reconcile'))")
        if not (await locked.fetchone())["pg_try_advisory_lock"]:
            logger.info("reconcile: another instance holds lock, skipping")
            return
        try:
            rows = await conn.execute(
                "SELECT id, status FROM sessions WHERE status IN ('running', 'pending')"
            )
            stuck = await rows.fetchall()
            for row in stuck:
                logger.warning(
                    "reconcile: marking stuck session %s (was %s) as failed",
                    row["id"],
                    row["status"],
                )
                await conn.execute(
                    "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s",
                    (row["id"],),
                )
        finally:
            await conn.execute("SELECT pg_advisory_unlock(hashtext('omniagent_reconcile'))")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from omniagent.api import sse_hub
    from omniagent.api.migrations import run_migrations
    from omniagent.worker.job import app as proc_app

    dsn = os.environ.get("DATABASE_URL", "")
    await run_migrations(dsn)
    await db.init_pool()
    await _reconcile_stuck_sessions()
    queue.set_session_fail_callback(_mark_session_failed)
    await sse_hub.start()

    async with proc_app.open_async():
        yield

    await sse_hub.stop()
    await db.close_pool()


app = FastAPI(title="OmniAgent Control Plane", lifespan=lifespan)


@app.middleware("http")
async def trace_and_metrics(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id", str(uuid.uuid4()))
    token = trace_id_var.set(trace_id)
    start = time.monotonic()
    try:
        response = await call_next(request)
        status = str(response.status_code)
    except Exception:
        status = "500"
        raise
    finally:
        route = request.scope.get("route")
        path = route.path if route else request.url.path
        REQUEST_LATENCY.labels(request.method, path).observe(time.monotonic() - start)
        REQUEST_COUNT.labels(request.method, path, status).inc()
        trace_id_var.reset(token)
    response.headers["X-Trace-Id"] = trace_id
    return response


@app.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    body, content_type = render_metrics()
    return Response(body, media_type=content_type)


@app.get("/health", include_in_schema=False)
async def health() -> JSONResponse:
    try:
        async with db.get_conn() as conn:
            await conn.execute("SELECT 1")
        return JSONResponse({"status": "ok", "db": "ok"})
    except Exception as e:
        return JSONResponse({"status": "error", "db": str(e)}, status_code=503)


_LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8">
<title>OmniAgent — Login</title>
<style>*{box-sizing:border-box;margin:0;padding:0}body{background:#0d0d0d;color:#ccc;font-family:system-ui,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh}
.card{background:#141414;border:1px solid #222;border-radius:8px;padding:32px;width:320px}
h1{font-size:16px;font-weight:600;margin-bottom:24px;color:#eee}
input{width:100%;background:#0d0d0d;border:1px solid #333;border-radius:4px;padding:8px 10px;color:#eee;font-size:14px;margin-bottom:16px;outline:none}
input:focus{border-color:#4f7ef8}
button{width:100%;background:#4f7ef8;border:none;border-radius:4px;padding:9px;color:#fff;font-size:14px;cursor:pointer}
button:hover{background:#3a6ae0}.err{color:#f87171;font-size:12px;margin-top:12px;display:none}</style>
</head><body><div class="card"><h1>OmniAgent</h1>
<input type="password" id="pw" placeholder="Password" autofocus onkeydown="if(event.key==='Enter')doLogin()">
<button onclick="doLogin()">Sign in</button>
<div class="err" id="err">Invalid password</div></div>
<script>async function doLogin(){const pw=document.getElementById('pw').value;
const r=await fetch('/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw})});
if(r.ok){location.href='/'}else{document.getElementById('err').style.display='block';document.getElementById('pw').value=''}}</script>
</body></html>"""


@app.get("/login", include_in_schema=False)
async def login_page(request: Request) -> HTMLResponse:
    from omniagent.api.routes.auth import validate_session

    if validate_session(request):
        return RedirectResponse("/")
    return HTMLResponse(_LOGIN_HTML)


@app.get("/", include_in_schema=False)
async def ui(request: Request) -> HTMLResponse:
    from omniagent.api.routes.auth import validate_session

    if not os.environ.get("UI_PASSWORD"):
        return HTMLResponse(
            "<h2>UI_PASSWORD is not set. Set it in your environment to enable the UI.</h2>",
            status_code=503,
        )
    if not validate_session(request):
        return RedirectResponse("/login")
    path = os.path.join(_UI_DIR, "index.html")
    with open(path) as f:
        html = f.read()
    return HTMLResponse(html)


app.include_router(auth.router)
app.include_router(tools.router)
app.include_router(namespaces.router)
app.include_router(toolboxes.router)
app.include_router(agents.router)
app.include_router(memory.router)
app.include_router(sessions.router)
app.include_router(schedules.router)
app.include_router(settings.router)
app.include_router(sse.router)
app.include_router(oauth2.router)
