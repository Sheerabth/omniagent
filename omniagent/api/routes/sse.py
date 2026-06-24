"""SSE streaming via PostgreSQL LISTEN/NOTIFY."""

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncGenerator

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from omniagent.api.auth import require_scope
from omniagent.api.db import get_conn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])

_CH = lambda sid: "session_" + str(sid).replace("-", "_")  # noqa: E731


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: uuid.UUID, _=Depends(require_scope("sessions:read"))
) -> EventSourceResponse:
    async with get_conn() as conn:
        rows = await conn.execute("SELECT status FROM sessions WHERE id = %s", (session_id,))
        sess = await rows.fetchone()
    if not sess:
        raise HTTPException(404)

    async def event_generator() -> AsyncGenerator[dict[str, str]]:
        dsn = os.environ.get("DATABASE_URL", "")
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as pg:
                # LISTEN before re-checking status to avoid race
                await pg.execute(f"LISTEN {_CH(session_id)}")

                async with get_conn() as conn:
                    rows = await conn.execute(
                        "SELECT status FROM sessions WHERE id = %s", (session_id,)
                    )
                    current = await rows.fetchone()

                if current and current["status"] in ("idle", "failed", "cancelled"):
                    ntype = "complete" if current["status"] == "idle" else current["status"]
                    yield {"data": json.dumps({"type": ntype})}
                    return

                try:
                    async with asyncio.timeout(300):
                        async for notify in pg.notifies():
                            ntype = notify.payload or "update"
                            yield {"data": json.dumps({"type": ntype})}
                            if ntype in ("complete", "error", "cancelled"):
                                break
                except TimeoutError:
                    yield {"data": json.dumps({"type": "error", "reason": "session timeout"})}
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("SSE error for session %s: %s", session_id, e)
            yield {"data": json.dumps({"type": "error", "reason": str(e)})}

    return EventSourceResponse(event_generator())
