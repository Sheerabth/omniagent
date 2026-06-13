from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import ToolRecord

router = APIRouter(prefix="/tools", tags=["tools"])


class ToolRegisterEntry(BaseModel):
    name: str
    description: str
    input_schema: dict
    output_schema: dict


class ToolRegisterRequest(BaseModel):
    namespace: str
    service: str
    execute_url: str
    tools: list[ToolRegisterEntry]


@router.post("/register", status_code=204)
async def register_tools(body: ToolRegisterRequest, _=Depends(require_any)):
    import json

    async with get_conn() as conn:
        # Check namespace collision: another service owns this namespace
        rows = await conn.execute(
            "SELECT DISTINCT service FROM tools WHERE namespace = %s AND service != %s",
            (body.namespace, body.service),
        )
        collision = await rows.fetchone()
        if collision:
            raise HTTPException(
                409,
                detail=f"Namespace '{body.namespace}' owned by service '{collision['service']}'",
            )

        for t in body.tools:
            await conn.execute(
                """
                INSERT INTO tools (name, namespace, service, description, input_schema, output_schema, execute_url, available)
                VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE)
                ON CONFLICT (name) DO UPDATE
                  SET description = EXCLUDED.description,
                      input_schema = EXCLUDED.input_schema,
                      output_schema = EXCLUDED.output_schema,
                      execute_url = EXCLUDED.execute_url,
                      available = TRUE,
                      updated_at = NOW()
                """,
                (
                    t.name,
                    body.namespace,
                    body.service,
                    t.description,
                    json.dumps(t.input_schema),
                    json.dumps(t.output_schema),
                    body.execute_url,
                ),
            )


@router.get("", response_model=list[ToolRecord])
async def list_tools(_=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT * FROM tools ORDER BY name")
        return await rows.fetchall()


@router.get("/{namespace}", response_model=list[ToolRecord])
async def list_tools_by_namespace(namespace: str, _=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT * FROM tools WHERE namespace = %s ORDER BY name",
            (namespace,),
        )
        return await rows.fetchall()
