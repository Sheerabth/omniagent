import uuid

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import AgentCreate, AgentPatch, AgentRecord

router = APIRouter(prefix="/agents", tags=["agents"])

VALID_HARNESSES = {"claude", "antigravity"}


async def _validate_skill_names(conn, skill_names: list[str]) -> None:
    if not skill_names:
        return
    rows = await conn.execute("SELECT name FROM skills WHERE name = ANY(%s)", (skill_names,))
    found = {r["name"] for r in await rows.fetchall()}
    missing = set(skill_names) - found
    if missing:
        raise HTTPException(400, detail=f"Unknown skills: {sorted(missing)}")


@router.post("", response_model=AgentRecord, status_code=201)
async def create_agent(body: AgentCreate, _=Depends(require_any)):
    if body.harness not in VALID_HARNESSES:
        raise HTTPException(400, detail=f"harness must be one of {VALID_HARNESSES}")
    async with get_conn() as conn:
        await _validate_skill_names(conn, body.skill_names)
        rows = await conn.execute(
            """
            INSERT INTO agents (name, harness, skill_names, system_prompt, use_monty)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (body.name, body.harness, body.skill_names, body.system_prompt, body.use_monty),
        )
        return await rows.fetchone()


@router.get("", response_model=list[AgentRecord])
async def list_agents(_=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM agents ORDER BY name")
        return await rows.fetchall()


@router.get("/{agent_id}", response_model=AgentRecord)
async def get_agent(agent_id: uuid.UUID, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM agents WHERE id = %s", (agent_id,))
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return row


@router.patch("/{agent_id}", response_model=AgentRecord)
async def patch_agent(agent_id: uuid.UUID, body: AgentPatch, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM agents WHERE id = %s", (agent_id,))
        existing = await rows.fetchone()
        if not existing:
            raise HTTPException(404)

        updates: dict = {}
        if body.skill_names is not None:
            await _validate_skill_names(conn, body.skill_names)
            updates["skill_names"] = body.skill_names
        if body.harness is not None:
            if body.harness not in VALID_HARNESSES:
                raise HTTPException(400, detail=f"harness must be one of {VALID_HARNESSES}")
            updates["harness"] = body.harness
        if body.system_prompt is not None:
            updates["system_prompt"] = body.system_prompt
        if body.use_monty is not None:
            updates["use_monty"] = body.use_monty

        if not updates:
            return existing

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        set_clause += ", updated_at = NOW()"
        values = list(updates.values()) + [agent_id]
        rows = await conn.execute(
            f"UPDATE agents SET {set_clause} WHERE id = %s RETURNING *",
            values,
        )
        return await rows.fetchone()


@router.delete("/{agent_id}", status_code=204)
async def delete_agent(agent_id: uuid.UUID, _=Depends(require_any)):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM agents WHERE id = %s", (agent_id,))
