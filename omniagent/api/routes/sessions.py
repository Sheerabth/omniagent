import json
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from omniagent.api.auth import require_scope
from omniagent.api.models import (
    FileRef,
    MessageRecord,
    ResumeRequest,
    RunRequest,
    SessionCreate,
    SessionRecord,
)
from omniagent.api.models import SessionStatus as SessionStatusResponse
from omniagent.api.models import (
    ToolCallEntry,
)
from omniagent.api.queries import (
    delete_session_by_id,
    insert_session,
    pg_notify,
    select_agent_by_name_version,
    select_agent_latest,
    select_session_for_update,
    select_session_full,
    select_sessions_recent,
    update_session_cancel_requested,
    update_session_messages_append,
    update_session_status_pending_returning,
)
from omniagent.config import settings
from omniagent.constants import NotifyType, SessionStatus, session_channel
from omniagent.db import get_conn
from omniagent.storage import StorageClient

router = APIRouter(prefix="/sessions", tags=["sessions"])

_storage = StorageClient()


@router.post("", response_model=SessionRecord, status_code=201)
async def create_session(
    body: SessionCreate, _=Depends(require_scope("sessions:write"))
) -> SessionRecord:
    async with get_conn() as conn:
        if body.agent_version:
            rows = await conn.execute(
                select_agent_by_name_version,
                {"name": body.agent_name, "version": body.agent_version},
            )
        else:
            rows = await conn.execute(
                select_agent_latest,
                {"name": body.agent_name},
            )
        agent = rows.mappings().fetchone()
        if not agent:
            raise HTTPException(404, detail="Agent not found")

        toolbox_versions = agent["toolbox_refs"] or {}
        tool_refs = agent["tool_refs"] or []

        rows = await conn.execute(
            insert_session,
            {
                "agent_name": body.agent_name,
                "agent_version": agent["version"],
                "toolbox_versions": json.dumps(toolbox_versions),
                "tool_refs": tool_refs,
            },
        )
        row = rows.mappings().fetchone()
        assert row is not None
        return SessionRecord.model_validate(dict(row))


@router.post("/{session_id}/run", status_code=202)
async def run_session(
    session_id: uuid.UUID, body: RunRequest, _=Depends(require_scope("sessions:write"))
) -> JSONResponse:
    """Append a user message and, if idle, kick off a turn.

    Safe to call while a turn is already in flight — the message is appended
    durably and picked up by the worker's catch-up check when the current
    turn completes, instead of being rejected. See job.py:_complete_session.
    """
    # Resolve file paths to FileRef metadata for the message record.
    file_refs: list[FileRef] = []
    if body.files:
        for fp in body.files:
            try:
                ref = await _storage.stat(str(session_id), fp)
                file_refs.append(ref)
            except Exception as exc:
                raise HTTPException(404, detail=f"File not found in session: {fp}") from exc

    new_message = MessageRecord(
        role="user",
        content=body.prompt,
        files=file_refs,
        timestamp=datetime.now(UTC).isoformat(),
    ).model_dump()

    async with get_conn() as conn:
        # Row lock so a concurrent /run can't race the busy-check below.
        rows = await conn.execute(select_session_for_update, {"id": session_id})
        session = rows.mappings().fetchone()
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
            {
                "msg": json.dumps([new_message]),
                "status": new_status,
                "id": session_id,
            },
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
        rows = await conn.execute(select_session_full, {"id": session_id})
        session = rows.mappings().fetchone()
        if not session:
            raise HTTPException(404)
        if session["status"] != SessionStatus.DEFERRED:
            raise HTTPException(409, detail="Session is not deferred")

        messages = session["messages"] or []
        resume_text = f"[RESUME: {body.message}]" if body.message else "[RESUME: Turn resumed.]"

        # Resolve file paths for the resume message.
        file_refs: list[FileRef] = []
        if body.files:
            for fp in body.files:
                try:
                    ref = await _storage.stat(str(session_id), fp)
                    file_refs.append(ref)
                except Exception as exc:
                    raise HTTPException(404, detail=f"File not found in session: {fp}") from exc

        messages.append(
            MessageRecord(
                role="user",
                content=resume_text,
                files=file_refs,
                timestamp=datetime.now(UTC).isoformat(),
            ).model_dump()
        )

        rows = await conn.execute(
            update_session_status_pending_returning,
            {
                "status": SessionStatus.PENDING,
                "messages": json.dumps(messages),
                "id": session_id,
                "where_status": SessionStatus.DEFERRED,
            },
        )
        if not rows.fetchone():
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
        return [SessionRecord.model_validate(dict(r)) for r in rows.mappings().fetchall()]


@router.post("/{session_id}/cancel", status_code=204)
async def cancel_session(session_id: uuid.UUID, _=Depends(require_scope("sessions:write"))) -> None:
    """Request cancellation — does not set status directly.

    status is job-owned (the worker is the only writer, always a confirmed
    fact about its own lifecycle). This just raises a flag the in-flight turn
    checks when it finishes; the worker decides the real outcome (cancelled,
    or straight into a follow-up turn if more was queued meanwhile) and
    writes status itself. See job.py:_complete_session / _handle_defer.
    """
    ch = session_channel(session_id)
    async with get_conn() as conn:
        await conn.execute(
            update_session_cancel_requested,
            {
                "id": session_id,
                "s1": SessionStatus.RUNNING,
                "s2": SessionStatus.PENDING,
                "s3": SessionStatus.DEFERRED,
            },
        )
        await conn.execute(pg_notify, {"channel": ch, "payload": NotifyType.CANCELLING})


@router.delete("/{session_id}", status_code=204)
async def delete_session(session_id: uuid.UUID, _=Depends(require_scope("sessions:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute(delete_session_by_id, {"id": session_id})
    # Cascade: remove all session files from MinIO (best-effort, log failures).
    try:
        await _storage.delete_prefix(str(session_id))
    except Exception:
        import logging

        logging.getLogger(__name__).warning(
            "Failed to delete MinIO files for session %s", session_id, exc_info=True
        )


@router.get("/{session_id}/status", response_model=SessionStatusResponse)
async def get_session_status(
    session_id: uuid.UUID, _=Depends(require_scope("sessions:read"))
) -> SessionStatusResponse:
    async with get_conn() as conn:
        rows = await conn.execute(select_session_full, {"id": session_id})
        session = rows.mappings().fetchone()
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


# ── File endpoints ─────────────────────────────────────────────────────────


@router.post("/{session_id}/files", response_model=FileRef, status_code=201)
async def upload_file(
    session_id: uuid.UUID,
    file: UploadFile,
    _=Depends(require_scope("sessions:write")),
) -> FileRef:
    """Upload a file to session storage. Max size controlled by config."""
    if not file.filename:
        raise HTTPException(400, detail="filename required")
    data = await file.read()
    ref = await _storage.upload(
        str(session_id),
        file.filename,
        data,
        file.content_type or "application/octet-stream",
    )
    ref.updated_at = datetime.now(UTC).isoformat()
    return ref


@router.get("/{session_id}/files", response_model=list[FileRef])
async def list_files(
    session_id: uuid.UUID,
    prefix: str = "",
    max_results: int = 200,
    _=Depends(require_scope("sessions:read")),
) -> list[FileRef]:
    """List files in session storage. Optional prefix filter."""
    return await _storage.list_objects(str(session_id), prefix=prefix, max_results=max_results)


@router.get("/{session_id}/files/{path:path}")
async def download_file(
    session_id: uuid.UUID,
    path: str,
    _=Depends(require_scope("sessions:read")),
) -> StreamingResponse:
    """Download a file from session storage."""
    import io

    try:
        data = await _storage.download(str(session_id), path)
        ref = await _storage.stat(str(session_id), path)
    except Exception as exc:
        raise HTTPException(404, detail=f"File not found: {path}") from exc

    return StreamingResponse(
        io.BytesIO(data),
        media_type=ref.content_type,
        headers={"Content-Disposition": f'inline; filename="{ref.name}"'},
    )


@router.delete("/{session_id}/files/{path:path}", status_code=204)
async def delete_file(
    session_id: uuid.UUID,
    path: str,
    _=Depends(require_scope("sessions:write")),
) -> None:
    """Delete a file from session storage."""
    try:
        await _storage.delete(str(session_id), path)
    except Exception as exc:
        raise HTTPException(404, detail=f"File not found: {path}") from exc
