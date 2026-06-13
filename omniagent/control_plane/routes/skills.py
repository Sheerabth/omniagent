import uuid

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import SkillCreate, SkillPatch, SkillRecord

router = APIRouter(prefix="/skills", tags=["skills"])


async def _validate_tool_names(conn, tool_names: list[str]) -> None:
    if not tool_names:
        return
    rows = await conn.execute("SELECT name FROM tools WHERE name = ANY(%s)", (tool_names,))
    found = {r["name"] for r in await rows.fetchall()}
    missing = set(tool_names) - found
    if missing:
        raise HTTPException(400, detail=f"Unknown tools: {sorted(missing)}")


@router.post("", response_model=SkillRecord, status_code=201)
async def create_skill(body: SkillCreate, _=Depends(require_any)):
    async with get_conn() as conn:
        await _validate_tool_names(conn, body.tool_names)
        rows = await conn.execute(
            """
            INSERT INTO skills (name, tool_names, instructions, system_prompt)
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (body.name, body.tool_names, body.instructions, body.system_prompt),
        )
        return (await rows.fetchone())


@router.get("", response_model=list[SkillRecord])
async def list_skills(_=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM skills ORDER BY name")
        return await rows.fetchall()


@router.get("/{skill_id}", response_model=SkillRecord)
async def get_skill(skill_id: uuid.UUID, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM skills WHERE id = %s", (skill_id,))
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return row


@router.patch("/{skill_id}", response_model=SkillRecord)
async def patch_skill(skill_id: uuid.UUID, body: SkillPatch, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM skills WHERE id = %s", (skill_id,))
        existing = await rows.fetchone()
        if not existing:
            raise HTTPException(404)

        updates: dict = {}
        if body.tool_names is not None:
            await _validate_tool_names(conn, body.tool_names)
            updates["tool_names"] = body.tool_names
        if body.instructions is not None:
            updates["instructions"] = body.instructions
        if body.system_prompt is not None:
            updates["system_prompt"] = body.system_prompt

        if not updates:
            return existing

        set_clause = ", ".join(f"{k} = %s" for k in updates)
        set_clause += ", updated_at = NOW()"
        values = list(updates.values()) + [skill_id]
        rows = await conn.execute(
            f"UPDATE skills SET {set_clause} WHERE id = %s RETURNING *",
            values,
        )
        return await rows.fetchone()


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: uuid.UUID, _=Depends(require_any)):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM skills WHERE id = %s", (skill_id,))
