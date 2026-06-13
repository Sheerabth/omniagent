"""Internal endpoints: worker only (worker key required)."""

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_worker
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import SessionEventRequest, SessionResultRequest
from omniagent.control_plane.redis_client import get_redis

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/sessions/{session_id}/result", status_code=204)
async def post_session_result(
    session_id: uuid.UUID,
    body: SessionResultRequest,
    _=Depends(require_worker),
):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT messages FROM sessions WHERE id = %s", (session_id,))
        sess = await rows.fetchone()
        if not sess:
            raise HTTPException(404)
        messages = sess["messages"] or []
        messages.append(
            {
                "role": "assistant",
                "content": body.result,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        await conn.execute(
            "UPDATE sessions SET status='complete', messages=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(messages), session_id),
        )

    await _publish(session_id, {"type": "complete", "result": body.result})


@router.post("/sessions/{session_id}/event", status_code=204)
async def post_session_event(
    session_id: uuid.UUID,
    body: SessionEventRequest,
    _=Depends(require_worker),
):
    if body.type == "error":
        async with get_conn() as conn:
            await conn.execute(
                "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s AND status='running'",
                (session_id,),
            )

    if body.type == "tool_result":
        event_data = body.model_dump(exclude_none=True)
        if event_data.get("input") is not None and event_data.get("output") is not None:
            async with get_conn() as conn:
                rows = await conn.execute(
                    "SELECT tool_calls FROM sessions WHERE id = %s", (session_id,)
                )
                sess = await rows.fetchone()
                if sess:
                    tool_calls = sess["tool_calls"] or []
                    tool_calls.append(
                        {
                            "tool_name": event_data.get("tool"),
                            "input": event_data.get("input"),
                            "output": event_data.get("output"),
                            "harness": event_data.get("harness"),
                            "timestamp": datetime.now(UTC).isoformat(),
                            "success": event_data.get("success", True),
                            "error": event_data.get("error"),
                        }
                    )
                    await conn.execute(
                        "UPDATE sessions SET tool_calls = %s WHERE id = %s",
                        (json.dumps(tool_calls), session_id),
                    )

    await _publish(session_id, body.model_dump(exclude_none=True))


async def _publish(session_id: uuid.UUID, payload: dict) -> None:
    channel = f"session_{session_id}"
    await get_redis().publish(channel, json.dumps(payload))
