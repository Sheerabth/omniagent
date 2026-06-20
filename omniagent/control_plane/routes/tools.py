from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import ToolRecord
from omniagent.control_plane.openapi import parse_spec

router = APIRouter(prefix="/tools", tags=["tools"])


class ImportOpenAPIRequest(BaseModel):
    spec: Any  # JSON dict or YAML string
    namespace: str
    base_url: str | None = None


@router.post("/import-openapi", status_code=201)
async def import_openapi(body: ImportOpenAPIRequest, _=Depends(require_any)) -> dict:
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
                """
                    INSERT INTO tools
                      (name, namespace, description, input_schema, output_schema,
                       openapi_method, openapi_path, openapi_base_url, openapi_security, timeout)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE
                      SET namespace        = EXCLUDED.namespace,
                          description      = EXCLUDED.description,
                          input_schema     = EXCLUDED.input_schema,
                          output_schema    = EXCLUDED.output_schema,
                          openapi_method   = EXCLUDED.openapi_method,
                          openapi_path     = EXCLUDED.openapi_path,
                          openapi_base_url = EXCLUDED.openapi_base_url,
                          openapi_security = EXCLUDED.openapi_security,
                          timeout          = EXCLUDED.timeout,
                          updated_at       = NOW()
                    """,
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
async def patch_tool(name: str, body: PatchToolRequest, _=Depends(require_any)) -> dict:
    async with get_conn() as conn:
        result = await conn.execute(
            "UPDATE tools SET timeout = %s, updated_at = NOW() WHERE name = %s",
            (body.timeout, name),
        )
        if result.rowcount == 0:
            raise HTTPException(404, detail=f"Tool {name!r} not found")
    return {"name": name, "timeout": body.timeout}


@router.delete("/{name}", status_code=204)
async def delete_tool(name: str, _=Depends(require_any)) -> None:
    async with get_conn() as conn:
        result = await conn.execute("DELETE FROM tools WHERE name = %s", (name,))
        if result.rowcount == 0:
            raise HTTPException(404, detail=f"Tool {name!r} not found")


@router.delete("/namespace/{namespace}", status_code=204)
async def delete_namespace(namespace: str, _=Depends(require_any)) -> None:
    async with get_conn() as conn:
        await conn.execute("DELETE FROM tools WHERE namespace = %s", (namespace,))


@router.get("", response_model=list[ToolRecord])
async def list_tools(_=Depends(require_any)) -> list[ToolRecord]:
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM tools ORDER BY name")
        return [ToolRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.get("/{namespace}", response_model=list[ToolRecord])
async def list_tools_by_namespace(namespace: str, _=Depends(require_any)) -> list[ToolRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM tools WHERE namespace = %s ORDER BY name",
            (namespace,),
        )
        return [ToolRecord.model_validate(dict(r)) for r in await rows.fetchall()]
