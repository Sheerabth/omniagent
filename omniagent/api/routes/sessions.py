import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from omniagent.api.auth import require_scope
from omniagent.api.db import get_conn
from omniagent.api.models import (
    MessageRecord,
    ResumeRequest,
    RunRequest,
    SessionCreate,
    SessionRecord,
    SessionStatus,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionRecord, status_code=201)
async def create_session(
    body: SessionCreate, _=Depends(require_scope("sessions:write"))
) -> SessionRecord:
    async with get_conn() as conn:
        if body.agent_version:
            rows = await conn.execute(
                "SELECT * FROM agents WHERE name = %s AND version = %s",
                (body.agent_name, body.agent_version),
            )
        else:
            rows = await conn.execute(
                "SELECT * FROM agents WHERE name = %s ORDER BY created_at DESC LIMIT 1",
                (body.agent_name,),
            )
        agent = await rows.fetchone()
        if not agent:
            raise HTTPException(404, detail="Agent not found")

        toolbox_versions = agent["toolbox_refs"] or {}
        tool_refs = agent["tool_refs"] or []

        rows = await conn.execute(
            """
            INSERT INTO sessions (agent_name, agent_version, toolbox_versions, tool_refs)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (body.agent_name, agent["version"], json.dumps(toolbox_versions), tool_refs),
        )
        return SessionRecord.model_validate(dict(await rows.fetchone()))


@router.post("/{session_id}/run", status_code=202)
async def run_session(
    session_id: uuid.UUID, body: RunRequest, _=Depends(require_scope("sessions:write"))
) -> JSONResponse:
    """Append a user message and, if idle, kick off a turn.

    Safe to call while a turn is already in flight — the message is appended
    durably and picked up by the worker's catch-up check when the current
    turn completes, instead of being rejected. See job.py:_complete_session.
    """
    new_message = MessageRecord(
        role="user",
        content=body.prompt,
        timestamp=datetime.now(UTC).isoformat(),
    ).model_dump()

    async with get_conn() as conn:
        # Row lock so a concurrent /run can't race the busy-check below.
        rows = await conn.execute(
            "SELECT status FROM sessions WHERE id = %s FOR UPDATE", (session_id,)
        )
        session = await rows.fetchone()
        if not session:
            raise HTTPException(404)

        was_idle = session["status"] in ("idle", "failed", "cancelled")
        new_status = "pending" if was_idle else session["status"]
        await conn.execute(
            """
            UPDATE sessions
            SET messages = messages || %s::jsonb, status = %s, updated_at = NOW()
            WHERE id = %s
            """,
            (json.dumps([new_message]), new_status, session_id),
        )

        if was_idle:
            from omniagent.worker.job import run_agent_job

            await run_agent_job.configure(queue="default").defer_async(
                session_id=str(session_id),
            )

    return JSONResponse({"session_id": str(session_id), "queued": not was_idle}, status_code=202)


@router.post("/{session_id}/resume", status_code=202)
async def resume_session(
    session_id: uuid.UUID, body: ResumeRequest, _=Depends(require_scope("sessions:write"))
) -> JSONResponse:
    """Inject a tool result into a deferred session and schedule the next turn."""
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,))
        session = await rows.fetchone()
        if not session:
            raise HTTPException(404)
        if session["status"] != "deferred":
            raise HTTPException(409, detail="Session is not deferred")

        messages = session["messages"] or []
        resume_text = f"[RESUME: {body.message}]" if body.message else "[RESUME: Turn resumed.]"
        messages.append(
            MessageRecord(
                role="user",
                content=resume_text,
                timestamp=datetime.now(UTC).isoformat(),
            ).model_dump()
        )

        rows = await conn.execute(
            """UPDATE sessions SET status='pending', messages=%s, updated_at=NOW()
               WHERE id=%s AND status='deferred' RETURNING id""",
            (json.dumps(messages), session_id),
        )
        if not await rows.fetchone():
            raise HTTPException(409, detail="Session is not deferred")

        from omniagent.worker.job import run_agent_job

        await run_agent_job.configure(queue="default").defer_async(
            session_id=str(session_id),
        )

    return JSONResponse({"session_id": str(session_id)}, status_code=202)


@router.get("", response_model=list[SessionRecord])
async def list_sessions(_=Depends(require_scope("sessions:read"))) -> list[SessionRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM sessions WHERE is_scheduled = FALSE ORDER BY created_at DESC LIMIT 100"
        )
        return [SessionRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.post("/{session_id}/cancel", status_code=204)
async def cancel_session(session_id: uuid.UUID, _=Depends(require_scope("sessions:write"))) -> None:
    """Request cancellation — does not set status directly.

    status is job-owned (the worker is the only writer, always a confirmed
    fact about its own lifecycle). This just raises a flag the in-flight turn
    checks when it finishes; the worker decides the real outcome (cancelled,
    or straight into a follow-up turn if more was queued meanwhile) and
    writes status itself. See job.py:_complete_session / _handle_defer.
    """
    ch = "session_" + str(session_id).replace("-", "_")
    async with get_conn() as conn:
        await conn.execute(
            "UPDATE sessions SET cancel_requested=true, updated_at=NOW() WHERE id=%s AND status IN ('running','pending','deferred')",
            (session_id,),
        )
        await conn.execute("SELECT pg_notify(%s, %s)", (ch, "cancelling"))


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: uuid.UUID, _=Depends(require_scope("sessions:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))


@router.get("/{session_id}/status", response_model=SessionStatus)
async def get_session_status(
    session_id: uuid.UUID, _=Depends(require_scope("sessions:read"))
) -> SessionStatus:
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,))
        session = await rows.fetchone()
    if not session:
        raise HTTPException(404)

    messages = session["messages"] or []
    last_assistant = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "assistant"),
        None,
    )
    return SessionStatus(
        status=session["status"],
        result=last_assistant,
        messages=messages,
        tool_calls=session["tool_calls"] or [],
        agent_name=session["agent_name"],
        agent_version=session["agent_version"],
        toolbox_versions=session["toolbox_versions"] or {},
        tool_refs=session["tool_refs"] or [],
    )
