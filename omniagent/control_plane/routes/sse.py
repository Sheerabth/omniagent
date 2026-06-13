"""SSE streaming via Postgres LISTEN/NOTIFY fan-out."""
import asyncio
import json
import logging
import uuid

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from psycopg import sql as pgsql
from sse_starlette.sse import EventSourceResponse

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
import os

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])


@router.get("/sessions/{session_id}/stream")
async def stream_session(session_id: uuid.UUID, _=Depends(require_any)):
    # Verify session exists
    async with get_conn() as conn:
        rows = await conn.execute("SELECT status FROM sessions WHERE id = %s", (session_id,))
        sess = await rows.fetchone()
    if not sess:
        raise HTTPException(404)

    # If already complete/failed, return immediately
    if sess["status"] in ("complete", "failed"):
        async def immediate():
            yield {
                "data": json.dumps({"type": sess["status"]}),
            }
        return EventSourceResponse(immediate())

    async def event_generator():
        dsn = os.environ["DATABASE_URL"]
        channel = f"session_{session_id}"
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as listen_conn:
                await listen_conn.execute(pgsql.SQL("LISTEN {}").format(pgsql.Identifier(channel)))
                async for notify in listen_conn.notifies():
                    try:
                        payload = json.loads(notify.payload)
                    except Exception:
                        continue
                    yield {"data": json.dumps(payload)}
                    if payload.get("type") in ("complete", "error"):
                        break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("SSE error for session %s: %s", session_id, e)
            yield {"data": json.dumps({"type": "error", "reason": str(e)})}

    return EventSourceResponse(event_generator())
