from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from omniagent.api.auth import require_scope
from omniagent.api.models import ToolRecord
from omniagent.api.openapi import parse_spec
from omniagent.api.queries import (
    delete_tool_by_name,
    delete_tools_by_namespace,
    select_all_tools,
    select_tools_by_namespace,
    update_tool_timeout,
    upsert_tool,
)
from omniagent.db import get_conn

router = APIRouter(prefix="/tools", tags=["tools"])


class ImportOpenAPIRequest(BaseModel):
    spec: Any  # JSON dict or YAML string
    namespace: str
    base_url: str | None = None


@router.post("/import-openapi", status_code=201)
async def import_openapi(
    body: ImportOpenAPIRequest, _=Depends(require_scope("tools:write"))
) -> dict:
    import json

    spec = body.spec
    if isinstance(spec, str):
        try:
            spec = yaml.safe_load(spec)
        except Exception as exc:
            raise HTTPException(400, detail=f"Invalid YAML/JSON spec: {exc}") from exc

    try:
        tools = parse_spec(spec, body.namespace, body.base_url)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    if not tools:
        raise HTTPException(422, detail="No operations found in spec")

    async with get_conn() as conn, conn.transaction():
        for t in tools:
            await conn.execute(
                upsert_tool,
                (
                    t.name,
                    body.namespace,
                    t.description,
                    json.dumps(t.input_schema),
                    json.dumps(t.output_schema),
                    t.openapi_method,
                    t.openapi_path,
                    t.openapi_base_url,
                    json.dumps(t.openapi_security) if t.openapi_security else None,
                    None,
                ),
            )

    return {"imported": len(tools), "tools": [t.name for t in tools]}


class PatchToolRequest(BaseModel):
    timeout: int | None = None


@router.patch("/{name}", status_code=200)
async def patch_tool(
    name: str, body: PatchToolRequest, _=Depends(require_scope("tools:write"))
) -> dict:
    async with get_conn() as conn:
        result = await conn.execute(update_tool_timeout, (body.timeout, name))
        if result.rowcount == 0:
            raise HTTPException(404, detail=f"Tool {name!r} not found")
    return {"name": name, "timeout": body.timeout}


@router.delete("/{name}", status_code=204)
async def delete_tool(name: str, _=Depends(require_scope("tools:write"))) -> None:
    async with get_conn() as conn:
        result = await conn.execute(delete_tool_by_name, (name,))
        if result.rowcount == 0:
            raise HTTPException(404, detail=f"Tool {name!r} not found")


@router.delete("/namespace/{namespace}", status_code=204)
async def delete_namespace(namespace: str, _=Depends(require_scope("tools:write"))) -> None:
    async with get_conn() as conn:
        await conn.execute(delete_tools_by_namespace, (namespace,))


@router.get("", response_model=list[ToolRecord])
async def list_tools(_=Depends(require_scope("tools:read"))) -> list[ToolRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(select_all_tools)
        return [ToolRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.get("/{namespace}", response_model=list[ToolRecord])
async def list_tools_by_namespace(
    namespace: str, _=Depends(require_scope("tools:read"))
) -> list[ToolRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(
            select_tools_by_namespace,
            (namespace,),
        )
        return [ToolRecord.model_validate(dict(r)) for r in await rows.fetchall()]
