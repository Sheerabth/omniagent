import uuid

from fastapi import APIRouter, Depends, HTTPException

from omniagent.api.auth import require_scope
from omniagent.api.models import VALID_SCOPES, ApiKeyCreate, ApiKeyRecord, ApiKeyResponse
from omniagent.api.queries import (
    delete_api_key_returning,
    insert_api_key_returning,
    select_api_key_by_id,
    select_non_ui_api_keys,
)
from omniagent.api.secrets import generate_key, hash_key
from omniagent.db import get_conn

router = APIRouter(prefix="/settings", tags=["settings"])


@router.post("/api-keys", status_code=201, response_model=ApiKeyResponse)
async def create_api_key(
    body: ApiKeyCreate, _=Depends(require_scope("keys:manage"))
) -> ApiKeyResponse:
    invalid = set(body.scopes) - VALID_SCOPES
    if invalid:
        raise HTTPException(400, detail=f"Invalid scopes: {sorted(invalid)}")
    key = generate_key()
    key_hash = hash_key(key)
    key_prefix = key[:8]
    async with get_conn() as conn:
        rows = await conn.execute(
            insert_api_key_returning,
            {
                "name": body.name,
                "key_hash": key_hash,
                "key_prefix": key_prefix,
                "scopes": body.scopes,
            },
        )
        row = rows.mappings().fetchone()
        assert row is not None, "INSERT RETURNING returned no row"
    return ApiKeyResponse(
        id=row["id"],
        name=row["name"],
        scopes=list(row["scopes"]),
        created_at=row["created_at"],
        key=key,
    )


@router.get("/api-keys", response_model=list[ApiKeyRecord])
async def list_api_keys(_=Depends(require_scope("keys:manage"))) -> list[ApiKeyRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(select_non_ui_api_keys)
        return [ApiKeyRecord.model_validate(dict(r)) for r in rows.mappings().fetchall()]


@router.delete("/api-keys/{key_id}", status_code=204)
async def delete_api_key(key_id: uuid.UUID, _=Depends(require_scope("keys:manage"))) -> None:
    async with get_conn() as conn:
        row = (await conn.execute(delete_api_key_returning, {"id": key_id})).fetchone()
        if not row:
            # Distinguish 404 vs 403: check if it exists at all
            exists = (await conn.execute(select_api_key_by_id, {"id": key_id})).fetchone()
            if not exists:
                raise HTTPException(404)
            raise HTTPException(403, detail="Cannot delete built-in UI key")
