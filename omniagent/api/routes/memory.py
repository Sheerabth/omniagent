import json

from fastapi import APIRouter, Depends

from omniagent.api.auth import require_scope
from omniagent.api.models import MemorySetRequest
from omniagent.api.queries import (
    delete_agent_memory_all,
    delete_agent_memory_key_value,
    select_agent_memory,
    upsert_agent_memory,
)
from omniagent.db import get_conn

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/{agent_name}")
async def get_agent_memory(agent_name: str, _=Depends(require_scope("agents:read"))) -> list[dict]:
    async with get_conn() as conn:
        rows = await conn.execute(select_agent_memory, (agent_name,))
        return [{"key": r["key"], "value": r["value"]} for r in await rows.fetchall()]


@router.put("/{agent_name}/{key}", status_code=204)
async def set_agent_memory(
    agent_name: str, key: str, body: MemorySetRequest, _=Depends(require_scope("agents:write"))
) -> None:
    async with get_conn() as conn:
        await conn.execute(
            upsert_agent_memory,
            (agent_name, key, json.dumps(body.value)),
        )


@router.delete("/{agent_name}/{key}", status_code=204)
async def delete_agent_memory_key(
    agent_name: str, key: str, _=Depends(require_scope("agents:write"))
) -> None:
    async with get_conn() as conn:
        await conn.execute(delete_agent_memory_key_value, (agent_name, key))


@router.delete("/{agent_name}", status_code=204)
async def clear_agent_memory(agent_name: str, _=Depends(require_scope("agents:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute(delete_agent_memory_all, (agent_name,))
