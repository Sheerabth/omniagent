import json
import os
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import (
    RunRequest,
    SessionCreate,
    SessionRecord,
    SessionStatus,
)

router = APIRouter(prefix="/sessions", tags=["sessions"])

MAX_HISTORY_TURNS = int(os.environ.get("MAX_HISTORY_TURNS", "50"))


async def _build_tool_snapshot(conn, agent_row: dict) -> dict:
    """Snapshot tool schemas for all skills attached to agent."""
    snapshot: dict = {}
    skill_names = agent_row.get("skill_names") or []
    if not skill_names:
        return snapshot

    rows = await conn.execute(
        "SELECT tool_names FROM skills WHERE name = ANY(%s)",
        (skill_names,),
    )
    all_tool_names = []
    for r in await rows.fetchall():
        all_tool_names.extend(r["tool_names"])

    if not all_tool_names:
        return snapshot

    rows = await conn.execute(
        "SELECT name, description, input_schema, output_schema FROM tools WHERE name = ANY(%s)",
        (all_tool_names,),
    )
    for r in await rows.fetchall():
        snapshot[r["name"]] = {
            "name": r["name"],
            "description": r["description"],
            "input_schema": r["input_schema"],
            "output_schema": r["output_schema"],
        }
    return snapshot


@router.post("", response_model=SessionRecord, status_code=201)
async def create_session(body: SessionCreate, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM agents WHERE id = %s", (body.agent_id,))
        agent = await rows.fetchone()
        if not agent:
            raise HTTPException(404, detail="Agent not found")

        snapshot = await _build_tool_snapshot(conn, agent)

        rows = await conn.execute(
            """
            INSERT INTO sessions (agent_id, tool_snapshot)
            VALUES (%s, %s)
            RETURNING id, agent_id, status, created_at
            """,
            (body.agent_id, json.dumps(snapshot)),
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

        rows = await conn.execute("SELECT * FROM agents WHERE id = %s", (session["agent_id"],))
        agent = await rows.fetchone()
        if not agent:
            raise HTTPException(500, detail="Agent not found for session")

        # Append user message and trim history
        messages = session["messages"] or []
        messages.append({
            "role": "user",
            "content": body.prompt,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
        if len(messages) > MAX_HISTORY_TURNS * 2:
            messages = messages[-(MAX_HISTORY_TURNS * 2):]

        # Fetch LLM key
        rows = await conn.execute(
            "SELECT encrypted_key FROM llm_keys WHERE harness = %s",
            (agent["harness"],),
        )
        llm_key_row = await rows.fetchone()
        llm_api_key: str | None = None
        if llm_key_row:
            from omniagent.control_plane.secrets import decrypt_llm_key
            llm_api_key = decrypt_llm_key(bytes(llm_key_row["encrypted_key"]))

        # Fetch skills
        skill_names = agent["skill_names"] or []
        skills = []
        if skill_names:
            rows = await conn.execute(
                "SELECT * FROM skills WHERE name = ANY(%s)",
                (skill_names,),
            )
            skills = await rows.fetchall()

        # Update session: status=running, append message
        await conn.execute(
            "UPDATE sessions SET status='running', messages=%s, updated_at=NOW() WHERE id=%s",
            (json.dumps(messages), session_id),
        )

        # Enqueue job
        from omniagent.worker.job import run_agent_job

        job_payload = {
            "session_id": str(session_id),
            "agent_config": {
                "harness": agent["harness"],
                "system_prompt": agent["system_prompt"],
                "skill_names": skill_names,
                "skills": [dict(s) for s in skills],
                "tool_snapshot": session["tool_snapshot"] or {},
                "use_monty": agent["use_monty"],
            },
            "llm_api_key": llm_api_key,
            "history": messages,
        }

        await run_agent_job.configure(queue="default").defer_async(
            session_id=str(session_id),
            payload=json.dumps(job_payload, default=str),
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
    )
