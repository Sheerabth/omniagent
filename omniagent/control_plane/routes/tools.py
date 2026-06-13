from fastapi import APIRouter, Depends

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import ToolRecord

router = APIRouter(prefix="/tools", tags=["tools"])


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
