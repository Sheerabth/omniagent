"""Internal endpoints: worker + service only (worker key required)."""
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_worker
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import (
    SessionEventRequest,
    SessionResultRequest,
    ToolExecuteRequest,
    ToolExecuteResponse,
)
from omniagent.control_plane.ws import execute_tool

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/tools/execute", response_model=ToolExecuteResponse)
async def execute_tool_endpoint(body: ToolExecuteRequest, _=Depends(require_worker)):
    try:
        output = await execute_tool(body.tool_name, body.input)
    except RuntimeError as e:
        err = str(e)
        if err.startswith("tool_unavailable"):
            raise HTTPException(503, detail={"error": "tool_unavailable", "tool": body.tool_name})
        if err.startswith("tool_timeout"):
            raise HTTPException(504, detail={"error": "tool_timeout", "tool": body.tool_name})
        raise HTTPException(500, detail=str(e))

    # Log ToolCallEntry to session
    async with get_conn() as conn:
        rows = await conn.execute("SELECT tool_calls FROM sessions WHERE id = %s", (body.session_id,))
        sess = await rows.fetchone()
        if sess:
            tool_calls = sess["tool_calls"] or []
            tool_calls.append({
                "tool_name": body.tool_name,
                "input": body.input,
                "output": output,
                "harness": "unknown",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "success": True,
                "error": None,
            })
            await conn.execute(
                "UPDATE sessions SET tool_calls = %s WHERE id = %s",
                (json.dumps(tool_calls), body.session_id),
            )

    return ToolExecuteResponse(output=output)


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
        messages.append({
            "role": "assistant",
            "content": body.result,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        await conn.execute(
            "UPDATE sessions SET status='complete', messages=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(messages), session_id),
        )

    await _pg_notify(session_id, {"type": "complete", "result": body.result})


@router.post("/sessions/{session_id}/event", status_code=204)
async def post_session_event(
    session_id: uuid.UUID,
    body: SessionEventRequest,
    _=Depends(require_worker),
):
    # Worker emitting error → mark session failed before notifying SSE subscribers
    if body.type == "error":
        async with get_conn() as conn:
            await conn.execute(
                "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s AND status='running'",
                (session_id,),
            )
    await _pg_notify(session_id, body.model_dump(exclude_none=True))


async def _pg_notify(session_id: uuid.UUID, payload: dict) -> None:
    import json as _json
    channel = f"session_{session_id}"
    async with get_conn() as conn:
        await conn.execute(
            f"SELECT pg_notify('{channel}', %s)",
            (_json.dumps(payload),),
        )
