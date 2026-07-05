from fastapi import APIRouter, Depends, HTTPException

from omniagent.api.auth import require_scope
from omniagent.api.models import ToolboxCreate, ToolboxRecord
from omniagent.api.queries import (
    delete_toolbox_by_name_version,
    select_all_toolboxes,
    select_tool_names_by_name_list,
    select_toolbox_by_name_and_version,
    select_toolbox_versions,
    upsert_toolbox,
)
from omniagent.db import DictConn, get_conn

router = APIRouter(prefix="/toolboxes", tags=["toolboxes"])


async def _validate_tool_names(conn: DictConn, tool_names: list[str]) -> None:
    if not tool_names:
        return
    rows = await conn.execute(select_tool_names_by_name_list, (tool_names,))
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
            upsert_toolbox,
            (body.name, body.version, body.tool_names, body.system_prompt),
        )
        row = await rows.fetchone()
        assert row is not None
        return ToolboxRecord.model_validate(row)


@router.get("", response_model=list[ToolboxRecord])
async def list_toolboxes(_=Depends(require_scope("toolboxes:read"))) -> list[ToolboxRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(select_all_toolboxes)
        return [ToolboxRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.get("/{name}", response_model=list[ToolboxRecord])
async def list_toolbox_versions(
    name: str, _=Depends(require_scope("toolboxes:read"))
) -> list[ToolboxRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(select_toolbox_versions, (name,))
        results = await rows.fetchall()
    if not results:
        raise HTTPException(404)
    return [ToolboxRecord.model_validate(dict(r)) for r in results]


@router.get("/{name}/{version}", response_model=ToolboxRecord)
async def get_toolbox(
    name: str, version: str, _=Depends(require_scope("toolboxes:read"))
) -> ToolboxRecord:
    async with get_conn() as conn:
        rows = await conn.execute(select_toolbox_by_name_and_version, (name, version))
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return ToolboxRecord.model_validate(dict(row))


@router.delete("/{name}/{version}", status_code=204)
async def delete_toolbox(
    name: str, version: str, _=Depends(require_scope("toolboxes:write"))
) -> None:
    async with get_conn() as conn:
        await conn.execute(delete_toolbox_by_name_version, (name, version))
