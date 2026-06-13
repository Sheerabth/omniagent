import json

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import AgentCreate, AgentRecord

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_HARNESSES = {"claude", "antigravity"}


async def _validate_skill_refs(conn, skill_refs: dict[str, str]) -> None:
    for skill_name, skill_version in skill_refs.items():
        rows = await conn.execute(
            "SELECT id FROM skills WHERE name = %s AND version = %s",
            (skill_name, skill_version),
        )
        if not await rows.fetchone():
            raise HTTPException(400, detail=f"Skill not found: {skill_name}:{skill_version}")


@router.post("", response_model=AgentRecord, status_code=201)
async def create_agent(body: AgentCreate, _=Depends(require_any)):
    if body.harness not in VALID_HARNESSES:
        raise HTTPException(400, detail=f"harness must be one of {VALID_HARNESSES}")
    async with get_conn() as conn:
        await _validate_skill_refs(conn, body.skill_refs)
        rows = await conn.execute(
            """
            INSERT INTO agents (name, version, harness, model, skill_refs, system_prompt, use_monty)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (name, version) DO UPDATE
              SET harness       = EXCLUDED.harness,
                  model         = EXCLUDED.model,
                  skill_refs    = EXCLUDED.skill_refs,
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
                json.dumps(body.skill_refs),
                body.system_prompt,
                body.use_monty,
            ),
        )
        return await rows.fetchone()


@router.get("", response_model=list[AgentRecord])
async def list_agents(_=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM agents ORDER BY name, created_at")
        return await rows.fetchall()


@router.get("/{name}", response_model=list[AgentRecord])
async def list_agent_versions(name: str, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM agents WHERE name = %s ORDER BY created_at", (name,)
        )
        results = await rows.fetchall()
    if not results:
        raise HTTPException(404)
    return results


@router.get("/{name}/{version}", response_model=AgentRecord)
async def get_agent(name: str, version: str, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM agents WHERE name = %s AND version = %s", (name, version)
        )
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return row


@router.delete("/{name}/{version}", status_code=204)
async def delete_agent(name: str, version: str, _=Depends(require_any)):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM agents WHERE name = %s AND version = %s", (name, version))
