"""FastAPI control plane."""
import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from omniagent.control_plane import db, queue as q
from omniagent.control_plane.auth import require_any
from omniagent.control_plane.routes import agents, internal, sessions, settings, skills, sse, tools
from omniagent.control_plane.ws import (
    register_connection,
    remove_connection,
    resolve_pending,
)

logger = logging.getLogger(__name__)


async def _mark_session_failed(session_id: str) -> None:
    import uuid
    sid = uuid.UUID(session_id)
    async with db.get_conn() as conn:
        await conn.execute(
            "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s AND status='running'",
            (sid,),
        )
    await internal._pg_notify(sid, {"type": "error", "reason": "job failed or timed out"})


async def _reconcile_stuck_sessions() -> None:
    """On restart: mark sessions stuck in 'running' as 'failed'."""
    async with db.get_conn() as conn:
        rows = await conn.execute(
            "SELECT id FROM sessions WHERE status = 'running'"
        )
        stuck = await rows.fetchall()
        for row in stuck:
            logger.warning("reconcile: marking stuck session %s as failed", row["id"])
            await conn.execute(
                "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s",
                (row["id"],),
            )


@asynccontextmanager
async def lifespan(app: FastAPI):
    from omniagent.worker.job import app as proc_app

    await db.init_pool()
    await _reconcile_stuck_sessions()
    q.set_session_fail_callback(_mark_session_failed)

    async with proc_app.open_async():
        yield

    await db.close_pool()


app = FastAPI(title="OmniAgent Control Plane", lifespan=lifespan)

app.include_router(tools.router)
app.include_router(skills.router)
app.include_router(agents.router)
app.include_router(sessions.router)
app.include_router(settings.router)
app.include_router(internal.router)
app.include_router(sse.router)


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    # Validate service key from header
    key = websocket.headers.get("X-OmniAgent-Key")
    if not key:
        await websocket.close(code=4001)
        return

    # Validate against service_keys table
    try:
        from omniagent.control_plane.db import get_conn
        from omniagent.control_plane.secrets import verify_key
        async with get_conn() as conn:
            rows = await conn.execute(
                "SELECT key_hash FROM service_keys WHERE key_prefix = %s", (key[:8],)
            )
            valid = False
            for row in await rows.fetchall():
                if verify_key(key, row["key_hash"]):
                    valid = True
                    break
        if not valid:
            await websocket.close(code=4001)
            return
    except Exception as e:
        logger.error("WS auth error: %s", e)
        await websocket.close(code=4001)
        return

    await websocket.accept()
    namespace: str | None = None

    _pong_event = asyncio.Event()

    async def _heartbeat():
        """Ping every 30s; close if no pong within 10s."""
        while True:
            await asyncio.sleep(30)
            try:
                _pong_event.clear()
                await websocket.send_text('{"type":"ping"}')
            except Exception:
                break
            try:
                await asyncio.wait_for(_pong_event.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("WS: no pong from namespace=%s, closing", namespace)
                await websocket.close(code=1001)
                break

    heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        async for raw in websocket.iter_text():
            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "register":
                namespace = msg["namespace"]
                service = msg["service"]
                tools_data = msg.get("tools", [])

                async with db.get_conn() as conn:
                    # Check namespace collision: another service owns this namespace
                    rows = await conn.execute(
                        "SELECT DISTINCT service FROM tools WHERE namespace = %s AND service != %s",
                        (namespace, service),
                    )
                    collision = await rows.fetchone()
                    if collision:
                        await websocket.send_text(json.dumps({
                            "type": "error",
                            "message": f"Namespace '{namespace}' owned by service '{collision['service']}'"
                        }))
                        await websocket.close(code=4009)
                        return

                    for t in tools_data:
                        await conn.execute(
                            """
                            INSERT INTO tools (name, namespace, service, description, input_schema, output_schema, available)
                            VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                            ON CONFLICT (name) DO UPDATE
                              SET description = EXCLUDED.description,
                                  input_schema = EXCLUDED.input_schema,
                                  output_schema = EXCLUDED.output_schema,
                                  available = TRUE,
                                  updated_at = NOW()
                            """,
                            (
                                t["name"], namespace, service,
                                t["description"],
                                json.dumps(t["input_schema"]),
                                json.dumps(t["output_schema"]),
                            ),
                        )

                await register_connection(namespace, websocket)
                logger.info("WS: %s/%s registered %d tools", service, namespace, len(tools_data))

            elif msg_type == "pong":
                _pong_event.set()

            elif msg_type == "execute_result":
                await resolve_pending(msg["request_id"], msg)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.error("WS error: %s", e)
    finally:
        heartbeat_task.cancel()
        if namespace:
            await remove_connection(namespace, websocket)
            async with db.get_conn() as conn:
                # Mark tools unavailable when connection drops
                # (they become available again on next register)
                await conn.execute(
                    "UPDATE tools SET available = FALSE WHERE namespace = %s",
                    (namespace,),
                )
