from fastapi import APIRouter, Depends, HTTPException

from omniagent.api.auth import require_scope
from omniagent.api.models import ToolboxCreate, ToolboxRecord
from omniagent.db import DictConn, get_conn

router = APIRouter(prefix="/toolboxes", tags=["toolboxes"])


async def _validate_tool_names(conn: DictConn, tool_names: list[str]) -> None:
    if not tool_names:
        return
    rows = await conn.execute("SELECT name FROM tools WHERE name = ANY(%s)", (tool_names,))
    found = {r["name"] for r in await rows.fetchall()}
    missing = set(tool_names) - found
    if missing:
        raise HTTPException(400, detail=f"Unknown tools: {sorted(missing)}")


@router.post("", response_model=ToolboxRecord, status_code=201)
async def create_toolbox(
    body: ToolboxCreate, _=Depends(require_scope("toolboxes:write"))
) -> ToolboxRecord:
    async with get_conn() as conn:
        await _validate_tool_names(conn, body.tool_names)
        rows = await conn.execute(
            """
            INSERT INTO toolboxes (name, version, tool_names, system_prompt)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (name, version) DO UPDATE
              SET tool_names    = EXCLUDED.tool_names,
                  system_prompt = EXCLUDED.system_prompt,
                  updated_at    = NOW()
            RETURNING *
            """,
            (body.name, body.version, body.tool_names, body.system_prompt),
        )
        row = await rows.fetchone()
        assert row is not None
        return ToolboxRecord.model_validate(row)


@router.get("", response_model=list[ToolboxRecord])
async def list_toolboxes(_=Depends(require_scope("toolboxes:read"))) -> list[ToolboxRecord]:
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM toolboxes ORDER BY name, created_at")
        return [ToolboxRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.get("/{name}", response_model=list[ToolboxRecord])
async def list_toolbox_versions(
    name: str, _=Depends(require_scope("toolboxes:read"))
) -> list[ToolboxRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM toolboxes WHERE name = %s ORDER BY created_at", (name,)
        )
        results = await rows.fetchall()
    if not results:
        raise HTTPException(404)
    return [ToolboxRecord.model_validate(dict(r)) for r in results]


@router.get("/{name}/{version}", response_model=ToolboxRecord)
async def get_toolbox(
    name: str, version: str, _=Depends(require_scope("toolboxes:read"))
) -> ToolboxRecord:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM toolboxes WHERE name = %s AND version = %s", (name, version)
        )
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return ToolboxRecord.model_validate(dict(row))


@router.delete("/{name}/{version}", status_code=204)
async def delete_toolbox(
    name: str, version: str, _=Depends(require_scope("toolboxes:write"))
) -> None:
    async with get_conn() as conn:
        await conn.execute(
            "DELETE FROM toolboxes WHERE name = %s AND version = %s", (name, version)
        )
