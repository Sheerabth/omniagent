import json

from fastapi import APIRouter, Depends

from omniagent.api.auth import require_scope
from omniagent.api.models import MemorySetRequest
from omniagent.db import get_conn

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/{agent_name}")
async def get_agent_memory(agent_name: str, _=Depends(require_scope("agents:read"))) -> list[dict]:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT key, value FROM agent_memory WHERE agent_name=%s ORDER BY key", (agent_name,)
        )
        return [{"key": r["key"], "value": r["value"]} for r in await rows.fetchall()]


@router.put("/{agent_name}/{key}", status_code=204)
async def set_agent_memory(
    agent_name: str, key: str, body: MemorySetRequest, _=Depends(require_scope("agents:write"))
) -> None:
    async with get_conn() as conn:
        await conn.execute(
            """INSERT INTO agent_memory (agent_name, key, value, updated_at)
               VALUES (%s, %s, %s::jsonb, NOW())
               ON CONFLICT (agent_name, key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
            (agent_name, key, json.dumps(body.value)),
        )


@router.delete("/{agent_name}/{key}", status_code=204)
async def delete_agent_memory_key(
    agent_name: str, key: str, _=Depends(require_scope("agents:write"))
) -> None:
    async with get_conn() as conn:
        await conn.execute(
            "DELETE FROM agent_memory WHERE agent_name=%s AND key=%s", (agent_name, key)
        )


@router.delete("/{agent_name}", status_code=204)
async def clear_agent_memory(agent_name: str, _=Depends(require_scope("agents:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute("DELETE FROM agent_memory WHERE agent_name=%s", (agent_name,))
