import json
import os
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import RunRequest, SessionCreate, SessionRecord, SessionStatus

router = APIRouter(prefix="/sessions", tags=["sessions"])

MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "50"))


@router.post("", response_model=SessionRecord, status_code=201)
async def create_session(body: SessionCreate, _=Depends(require_any)):
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

        skill_versions = agent["skill_refs"] or {}

        rows = await conn.execute(
            """
            INSERT INTO sessions (agent_name, agent_version, skill_versions)
            VALUES (%s, %s, %s)
            RETURNING *
            """,
            (body.agent_name, agent["version"], json.dumps(skill_versions)),
        )
        return await rows.fetchone()


@router.post("/{session_id}/run", status_code=202)
async def run_session(session_id: uuid.UUID, body: RunRequest, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM sessions WHERE id = %s", (session_id,))
        session = await rows.fetchone()
        if not session:
            raise HTTPException(404)
        if session["status"] == "running":
            raise HTTPException(409, detail="Session is already running")

        messages = session["messages"] or []
        messages.append(
            {
                "role": "user",
                "content": body.prompt,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        if len(messages) > MAX_HISTORY_TURNS * 2:
            messages = messages[-(MAX_HISTORY_TURNS * 2) :]

        await conn.execute(
            "UPDATE sessions SET status='running', messages=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(messages), session_id),
        )

        from omniagent.worker.job import run_agent_job

        await run_agent_job.configure(queue="default").defer_async(
            session_id=str(session_id),
            payload=json.dumps({"history": messages}, default=str),
        )

    return JSONResponse({"session_id": str(session_id)}, status_code=202)


@router.get("/{session_id}/status", response_model=SessionStatus)
async def get_session_status(session_id: uuid.UUID, _=Depends(require_any)):
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
        skill_versions=session["skill_versions"] or {},
    )
