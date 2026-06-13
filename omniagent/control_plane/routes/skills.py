from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import SkillCreate, SkillRecord

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
            INSERT INTO skills (name, version, tool_names, instructions, system_prompt)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (name, version) DO UPDATE
              SET tool_names    = EXCLUDED.tool_names,
                  instructions  = EXCLUDED.instructions,
                  system_prompt = EXCLUDED.system_prompt,
                  updated_at    = NOW()
            RETURNING *
            """,
            (body.name, body.version, body.tool_names, body.instructions, body.system_prompt),
        )
        return await rows.fetchone()


@router.get("", response_model=list[SkillRecord])
async def list_skills(_=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM skills ORDER BY name, created_at")
        return await rows.fetchall()


@router.get("/{name}", response_model=list[SkillRecord])
async def list_skill_versions(name: str, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM skills WHERE name = %s ORDER BY created_at", (name,)
        )
        results = await rows.fetchall()
    if not results:
        raise HTTPException(404)
    return results


@router.get("/{name}/{version}", response_model=SkillRecord)
async def get_skill(name: str, version: str, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM skills WHERE name = %s AND version = %s", (name, version)
        )
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return row


@router.delete("/{name}/{version}", status_code=204)
async def delete_skill(name: str, version: str, _=Depends(require_any)):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM skills WHERE name = %s AND version = %s", (name, version))
