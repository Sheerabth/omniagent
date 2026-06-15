import uuid

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import ApiKeyCreate, ApiKeyRecord, ApiKeyResponse
from omniagent.control_plane.secrets import generate_key, hash_key

router = APIRouter(prefix="/settings", tags=["settings"])


# ── API keys ────────────────────────────────────────────────────────────────


@router.post("/api-keys", status_code=201, response_model=ApiKeyResponse)
async def create_api_key(body: ApiKeyCreate, _=Depends(require_any)) -> ApiKeyResponse:
    key = generate_key()
    key_hash = hash_key(key)
    key_prefix = key[:8]
    async with get_conn() as conn:
        rows = await conn.execute(
            "INSERT INTO api_keys (name, key_hash, key_prefix) VALUES (%s, %s, %s) RETURNING id, name, created_at",
            (body.name, key_hash, key_prefix),
        )
        row = await rows.fetchone()
    return ApiKeyResponse(id=row["id"], name=row["name"], created_at=row["created_at"], key=key)


@router.get("/api-keys", response_model=list[ApiKeyRecord])
async def list_api_keys(_=Depends(require_any)) -> list[ApiKeyRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT id, name, created_at FROM api_keys WHERE name != '_built-in-ui' ORDER BY created_at"
        )
        return [ApiKeyRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key(key_id: uuid.UUID, _=Depends(require_any)) -> None:
    async with get_conn() as conn:
        row = await conn.execute("SELECT name FROM api_keys WHERE id = %s", (key_id,))
        key = await row.fetchone()
        if not key:
            raise HTTPException(404)
        if key["name"] == "_built-in-ui":
            raise HTTPException(403, detail="Cannot delete built-in UI key")
        await conn.execute("DELETE FROM api_keys WHERE id = %s", (key_id,))
