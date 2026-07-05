import json
import logging

from fastapi import APIRouter, Depends, HTTPException

from omniagent.api.auth import require_scope
from omniagent.api.models import AgentCreate, AgentRecord
from omniagent.api.queries import (
    delete_agent_by_name_version,
    select_agent_by_name_version,
    select_agent_versions,
    select_all_agents,
    select_tool_by_name,
    select_toolbox_by_name_version,
    upsert_agent,
)
from omniagent.constants import HarnessName
from omniagent.db import DictConn, get_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_HARNESSES = {h.value for h in HarnessName}


async def _validate_toolbox_refs(conn: DictConn, toolbox_refs: dict[str, str]) -> None:
    for toolbox_name, toolbox_version in toolbox_refs.items():
        rows = await conn.execute(
            select_toolbox_by_name_version,
            (toolbox_name, toolbox_version),
        )
        if not await rows.fetchone():
            raise HTTPException(400, detail=f"Toolbox not found: {toolbox_name}:{toolbox_version}")


async def _validate_tool_refs(conn: DictConn, tool_refs: list[str]) -> None:
    for tool_name in tool_refs:
        rows = await conn.execute(select_tool_by_name, (tool_name,))
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
            upsert_agent,
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
        rows = await conn.execute(select_all_agents)
        return [AgentRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.get("/{name}", response_model=list[AgentRecord])
async def list_agent_versions(
    name: str, _=Depends(require_scope("agents:read"))
) -> list[AgentRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(select_agent_versions, (name,))
        results = await rows.fetchall()
    if not results:
        raise HTTPException(404)
    return [AgentRecord.model_validate(dict(r)) for r in results]


@router.get("/{name}/{version}", response_model=AgentRecord)
async def get_agent(
    name: str, version: str, _=Depends(require_scope("agents:read"))
) -> AgentRecord:
    async with get_conn() as conn:
        rows = await conn.execute(select_agent_by_name_version, (name, version))
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return AgentRecord.model_validate(dict(row))


@router.delete("/{name}/{version}", status_code=204)
async def delete_agent(name: str, version: str, _=Depends(require_scope("agents:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute(delete_agent_by_name_version, (name, version))
