"""Internal endpoints: worker only (worker key required)."""

import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_internal
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import (
    MessageRecord,
    SessionEventRequest,
    SessionResultRequest,
    ToolCallEntry,
)

router = APIRouter(prefix="/internal", tags=["internal"])

_CH = lambda sid: "session_" + str(sid).replace("-", "_")  # noqa: E731


@router.post("/sessions/{session_id}/result", status_code=204)
async def post_session_result(
    session_id: uuid.UUID,
    body: SessionResultRequest,
    _=Depends(require_internal),
) -> None:
    async with get_conn() as conn:
        rows = await conn.execute("SELECT messages FROM sessions WHERE id = %s", (session_id,))
        sess = await rows.fetchone()
        if not sess:
            raise HTTPException(404)
        messages = sess["messages"] or []
        messages.append(
            MessageRecord(
                role="assistant",
                content=body.result,
                timestamp=datetime.now(UTC).isoformat(),
            ).model_dump()
        )
        await conn.execute(
            "UPDATE sessions SET status='complete', messages=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(messages), session_id),
        )
        await conn.execute("SELECT pg_notify(%s, %s)", (_CH(session_id), "complete"))


@router.post("/sessions/{session_id}/event", status_code=204)
async def post_session_event(
    session_id: uuid.UUID,
    body: SessionEventRequest,
    _=Depends(require_internal),
) -> None:
    ntype = "error" if body.type == "error" else "update"

    if body.type == "error":
        async with get_conn() as conn:
            await conn.execute(
                "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s AND status='running'",
                (session_id,),
            )
            await conn.execute("SELECT pg_notify(%s, %s)", (_CH(session_id), ntype))

    elif body.type == "tool_result":
        event_data = body.model_dump(exclude_none=True)
        if event_data.get("input") is not None and "output" in event_data:
            async with get_conn() as conn:
                rows = await conn.execute(
                    "SELECT tool_calls FROM sessions WHERE id = %s", (session_id,)
                )
                sess = await rows.fetchone()
                if sess:
                    tool_calls = sess["tool_calls"] or []
                    tool_calls.append(
                        ToolCallEntry(
                            tool_name=event_data.get("tool") or "",
                            input=event_data.get("input") or {},
                            output=event_data.get("output"),
                            harness=event_data.get("harness"),
                            skill_name=event_data.get("skill_name"),
                            timestamp=datetime.now(UTC),
                            success=event_data.get("success", True),
                            error=event_data.get("error"),
                        ).model_dump(mode="json")
                    )
                    await conn.execute(
                        "UPDATE sessions SET tool_calls = %s WHERE id = %s",
                        (json.dumps(tool_calls), session_id),
                    )
                    await conn.execute("SELECT pg_notify(%s, %s)", (_CH(session_id), ntype))
    else:
        async with get_conn() as conn:
            await conn.execute("SELECT pg_notify(%s, %s)", (_CH(session_id), body.type))


async def _notify(session_id: uuid.UUID, ntype: str) -> None:
    async with get_conn() as conn:
        await conn.execute("SELECT pg_notify(%s, %s)", (_CH(session_id), ntype))
