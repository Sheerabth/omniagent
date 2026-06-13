"""SSE streaming via Redis pub/sub fan-out."""
import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.redis_client import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])


@router.get("/sessions/{session_id}/stream")
async def stream_session(session_id: uuid.UUID, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT status FROM sessions WHERE id = %s", (session_id,))
        sess = await rows.fetchone()
    if not sess:
        raise HTTPException(404)

    if sess["status"] in ("complete", "failed"):
        async def immediate():
            async with get_conn() as conn:
                rows = await conn.execute("SELECT messages FROM sessions WHERE id = %s", (session_id,))
                s = await rows.fetchone()
            messages = (s and s["messages"]) or []
            last = next((m["content"] for m in reversed(messages) if m.get("role") == "assistant"), None)
            if sess["status"] == "complete":
                yield {"data": json.dumps({"type": "complete", "result": last})}
            else:
                yield {"data": json.dumps({"type": "error", "reason": "session failed"})}
        return EventSourceResponse(immediate())

    async def event_generator():
        channel = f"session_{session_id}"
        pubsub = get_redis().pubsub()
        try:
            await pubsub.subscribe(channel)
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
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
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return EventSourceResponse(event_generator())
