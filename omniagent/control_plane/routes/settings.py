import uuid

from fastapi import APIRouter, Depends, HTTPException

from omniagent.control_plane.auth import require_any
from omniagent.control_plane.db import get_conn
from omniagent.control_plane.models import (
    ClientKeyCreate,
    ClientKeyRecord,
    LlmKeyCreate,
    LlmKeyRecord,
    ServiceKeyCreate,
    ServiceKeyRecord,
)
from omniagent.control_plane.secrets import (
    decrypt_llm_key,
    encrypt_llm_key,
    generate_key,
    hash_key,
)

router = APIRouter(prefix="/settings", tags=["settings"])


# ── Client keys ────────────────────────────────────────────────────────────

@router.post("/client-keys", status_code=201)
async def create_client_key(body: ClientKeyCreate, _=Depends(require_any)):
    key = generate_key()
    key_hash = hash_key(key)
    key_prefix = key[:8]
    async with get_conn() as conn:
        rows = await conn.execute(
            "INSERT INTO client_keys (name, key_hash, key_prefix) VALUES (%s, %s, %s) RETURNING id, name, created_at",
            (body.name, key_hash, key_prefix),
        )
        row = await rows.fetchone()
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "key": key}


@router.get("/client-keys", response_model=list[ClientKeyRecord])
async def list_client_keys(_=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT id, name, created_at FROM client_keys ORDER BY created_at")
        return await rows.fetchall()


@router.delete("/client-keys/{key_id}", status_code=204)
async def delete_client_key(key_id: uuid.UUID, _=Depends(require_any)):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM client_keys WHERE id = %s", (key_id,))


# ── Service keys ───────────────────────────────────────────────────────────

@router.post("/service-keys", status_code=201)
async def create_service_key(body: ServiceKeyCreate, _=Depends(require_any)):
    key = generate_key()
    key_hash = hash_key(key)
    key_prefix = key[:8]
    async with get_conn() as conn:
        rows = await conn.execute(
            "INSERT INTO service_keys (name, key_hash, key_prefix) VALUES (%s, %s, %s) RETURNING id, name, created_at",
            (body.name, key_hash, key_prefix),
        )
        row = await rows.fetchone()
    return {"id": row["id"], "name": row["name"], "created_at": row["created_at"], "key": key}


@router.get("/service-keys", response_model=list[ServiceKeyRecord])
async def list_service_keys(_=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT id, name, created_at FROM service_keys ORDER BY created_at")
        return await rows.fetchall()


@router.delete("/service-keys/{key_id}", status_code=204)
async def delete_service_key(key_id: uuid.UUID, _=Depends(require_any)):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM service_keys WHERE id = %s", (key_id,))


# ── LLM API keys ───────────────────────────────────────────────────────────

@router.post("/keys", status_code=201)
async def create_llm_key(body: LlmKeyCreate, _=Depends(require_any)):
    encrypted = encrypt_llm_key(body.api_key)
    hint = body.api_key[-4:]
    async with get_conn() as conn:
        await conn.execute(
            """
            INSERT INTO llm_keys (harness, encrypted_key, key_hint)
            VALUES (%s, %s, %s)
            ON CONFLICT (harness) DO UPDATE
              SET encrypted_key = EXCLUDED.encrypted_key,
                  key_hint = EXCLUDED.key_hint,
                  created_at = NOW()
            """,
            (body.harness, encrypted, hint),
        )
    return LlmKeyRecord(harness=body.harness, key_hint=hint)


@router.get("/keys", response_model=list[LlmKeyRecord])
async def list_llm_keys(_=Depends(require_any)):
    async with get_conn() as conn:
        rows = await conn.execute("SELECT harness, key_hint FROM llm_keys ORDER BY harness")
        return await rows.fetchall()


@router.delete("/keys/{harness}", status_code=204)
async def delete_llm_key(harness: str, _=Depends(require_any)):
    async with get_conn() as conn:
        await conn.execute("DELETE FROM llm_keys WHERE harness = %s", (harness,))
