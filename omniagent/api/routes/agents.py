import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from omniagent.api.auth import require_scope
from omniagent.api.db import DictConn, get_conn
from omniagent.api.models import AgentCreate, AgentRecord

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_HARNESSES = {"claude", "antigravity"}


async def _validate_toolbox_refs(conn: DictConn, toolbox_refs: dict[str, str]) -> None:
    for toolbox_name, toolbox_version in toolbox_refs.items():
        rows = await conn.execute(
            "SELECT id FROM toolboxes WHERE name = %s AND version = %s",
            (toolbox_name, toolbox_version),
        )
        if not await rows.fetchone():
            raise HTTPException(400, detail=f"Toolbox not found: {toolbox_name}:{toolbox_version}")


async def _validate_tool_refs(conn: DictConn, tool_refs: list[str]) -> None:
    for tool_name in tool_refs:
        rows = await conn.execute("SELECT name FROM tools WHERE name = %s", (tool_name,))
        if not await rows.fetchone():
            raise HTTPException(400, detail=f"Tool not found: {tool_name}")


@router.post("", response_model=AgentRecord, status_code=201)
async def create_agent(body: AgentCreate, _=Depends(require_scope("agents:write"))) -> AgentRecord:
    if body.harness not in VALID_HARNESSES:
        raise HTTPException(400, detail=f"harness must be one of {VALID_HARNESSES}")
    async with get_conn() as conn:
        await _validate_toolbox_refs(conn, body.toolbox_refs)
        await _validate_tool_refs(conn, body.tool_refs)
        rows = await conn.execute(
            """
            INSERT INTO agents (name, version, harness, model, toolbox_refs, tool_refs, system_prompt, use_monty)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name, version) DO UPDATE
              SET harness       = EXCLUDED.harness,
                  model         = EXCLUDED.model,
                  toolbox_refs  = EXCLUDED.toolbox_refs,
                  tool_refs     = EXCLUDED.tool_refs,
                  system_prompt = EXCLUDED.system_prompt,
                  use_monty     = EXCLUDED.use_monty,
                  updated_at    = NOW()
            RETURNING *
            """,
            (
                body.name,
                body.version,
                body.harness,
                body.model,
                json.dumps(body.toolbox_refs),
                body.tool_refs,
                body.system_prompt,
                body.use_monty,
            ),
        )
        row = await rows.fetchone()
        assert row is not None
        return AgentRecord.model_validate(row)


@router.get("", response_model=list[AgentRecord])
async def list_agents(_=Depends(require_scope("agents:read"))) -> list[AgentRecord]:
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM agents ORDER BY name, created_at")
        return [AgentRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.get("/{name}", response_model=list[AgentRecord])
async def list_agent_versions(
    name: str, _=Depends(require_scope("agents:read"))
) -> list[AgentRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM agents WHERE name = %s ORDER BY created_at", (name,)
        )
        results = await rows.fetchall()
    if not results:
        raise HTTPException(404)
    return [AgentRecord.model_validate(dict(r)) for r in results]


@router.get("/{name}/{version}", response_model=AgentRecord)
async def get_agent(
    name: str, version: str, _=Depends(require_scope("agents:read"))
) -> AgentRecord:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM agents WHERE name = %s AND version = %s", (name, version)
        )
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return AgentRecord.model_validate(dict(row))


@router.delete("/{name}/{version}", status_code=204)
async def delete_agent(name: str, version: str, _=Depends(require_scope("agents:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute("DELETE FROM agents WHERE name = %s AND version = %s", (name, version))
