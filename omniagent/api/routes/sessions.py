import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from omniagent.api.auth import require_scope
from omniagent.api.models import (
    MessageRecord,
    ResumeRequest,
    RunRequest,
    SessionCreate,
    SessionRecord,
)
from omniagent.api.models import SessionStatus as SessionStatusResponse
from omniagent.api.models import ToolCallEntry
from omniagent.api.queries import (
    build_update_session_status_pending_returning,
    delete_session_by_id,
    insert_session,
    select_agent_by_name_version,
    select_agent_latest,
    select_session_for_update,
    select_session_full,
    select_sessions_recent,
    update_session_cancel_requested,
    update_session_messages_append,
)
from omniagent.config import settings
from omniagent.constants import NotifyType, SessionStatus, session_channel
from omniagent.db import get_conn

router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.post("", response_model=SessionRecord, status_code=201)
async def create_session(
    body: SessionCreate, _=Depends(require_scope("sessions:write"))
) -> SessionRecord:
    async with get_conn() as conn:
        if body.agent_version:
            rows = await conn.execute(
                select_agent_by_name_version,
                (body.agent_name, body.agent_version),
            )
        else:
            rows = await conn.execute(
                select_agent_latest,
                (body.agent_name,),
            )
        agent = await rows.fetchone()
        if not agent:
            raise HTTPException(404, detail="Agent not found")

        toolbox_versions = agent["toolbox_refs"] or {}
        tool_refs = agent["tool_refs"] or []

        rows = await conn.execute(
            insert_session,
            (body.agent_name, agent["version"], json.dumps(toolbox_versions), tool_refs),
        )
        row = await rows.fetchone()
        assert row is not None
        return SessionRecord.model_validate(row)


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
        rows = await conn.execute(select_session_for_update, (session_id,))
        session = await rows.fetchone()
        if not session:
            raise HTTPException(404)

        was_idle = session["status"] in (
            SessionStatus.IDLE,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        )
        new_status = SessionStatus.PENDING if was_idle else session["status"]
        await conn.execute(
            update_session_messages_append,
            (json.dumps([new_message]), new_status, session_id),
        )

        if was_idle:
            from omniagent.worker.job import run_agent_job

            await run_agent_job.configure(queue=settings.worker_queue_name).defer_async(
                session_id=str(session_id),
            )

    return JSONResponse({"session_id": str(session_id), "queued": not was_idle}, status_code=202)


@router.post("/{session_id}/resume", status_code=202)
async def resume_session(
    session_id: uuid.UUID, body: ResumeRequest, _=Depends(require_scope("sessions:write"))
) -> JSONResponse:
    """Inject a tool result into a deferred session and schedule the next turn."""
    async with get_conn() as conn:
        rows = await conn.execute(select_session_full, (session_id,))
        session = await rows.fetchone()
        if not session:
            raise HTTPException(404)
        if session["status"] != SessionStatus.DEFERRED:
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
            build_update_session_status_pending_returning(
                SessionStatus.DEFERRED
            ),  # pyright: ignore[reportArgumentType]
            (json.dumps(messages), session_id),
        )
        if not await rows.fetchone():
            raise HTTPException(409, detail="Session is not deferred")

        from omniagent.worker.job import run_agent_job

        await run_agent_job.configure(queue=settings.worker_queue_name).defer_async(
            session_id=str(session_id),
        )

    return JSONResponse({"session_id": str(session_id)}, status_code=202)


@router.get("", response_model=list[SessionRecord])
async def list_sessions(_=Depends(require_scope("sessions:read"))) -> list[SessionRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(select_sessions_recent)
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
    from omniagent.api.queries import pg_notify

    ch = session_channel(session_id)
    async with get_conn() as conn:
        await conn.execute(
            update_session_cancel_requested,
            (session_id, SessionStatus.RUNNING, SessionStatus.PENDING, SessionStatus.DEFERRED),
        )
        await conn.execute(pg_notify, (ch, NotifyType.CANCELLING))


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: uuid.UUID, _=Depends(require_scope("sessions:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute(delete_session_by_id, (session_id,))


@router.get("/{session_id}/status", response_model=SessionStatusResponse)
async def get_session_status(
    session_id: uuid.UUID, _=Depends(require_scope("sessions:read"))
) -> SessionStatusResponse:
    async with get_conn() as conn:
        rows = await conn.execute(select_session_full, (session_id,))
        session = await rows.fetchone()
    if not session:
        raise HTTPException(404)

    messages = session["messages"] or []
    last_assistant = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "assistant"),
        None,
    )
    tool_calls = [ToolCallEntry.model_validate(tc) for tc in (session["tool_calls"] or [])]
    return SessionStatusResponse(
        status=session["status"],
        result=last_assistant,
        messages=messages,
        tool_calls=tool_calls,
        agent_name=session["agent_name"],
        agent_version=session["agent_version"],
        toolbox_versions=session["toolbox_versions"] or {},
        tool_refs=session["tool_refs"] or [],
    )
