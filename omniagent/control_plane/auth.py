"""X-OmniAgent-Key validation middleware.

Key types:
  worker  — matches OMNIAGENT_WORKER_SECRET env var directly
  client  — argon2 hash in client_keys table
  service — argon2 hash in service_keys table

Key prefix (first 8 chars) is stored alongside hash to avoid O(n) argon2 scan.
"""
import os
from typing import Literal

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from omniagent.control_plane.db import get_conn
from omniagent.control_plane.secrets import verify_key

_header_scheme = APIKeyHeader(name="X-OmniAgent-Key", auto_error=False)

KeyType = Literal["worker", "client", "service"]


async def _resolve_key(key: str) -> KeyType:
    worker_secret = os.environ.get("OMNIAGENT_WORKER_SECRET", "")
    if worker_secret and key == worker_secret:
        return "worker"

    prefix = key[:8]
    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT key_hash FROM client_keys WHERE key_prefix = %s", (prefix,)
        )
        for row in await rows.fetchall():
            if verify_key(key, row["key_hash"]):
                return "client"

        rows = await conn.execute(
            "SELECT key_hash FROM service_keys WHERE key_prefix = %s", (prefix,)
        )
        for row in await rows.fetchall():
            if verify_key(key, row["key_hash"]):
                return "service"

    raise HTTPException(status_code=401, detail="Invalid X-OmniAgent-Key")


async def require_any(request: Request, api_key: str | None = Security(_header_scheme)) -> KeyType:
    if not api_key:
        raise HTTPException(status_code=401, detail="X-OmniAgent-Key header missing")
    return await _resolve_key(api_key)


async def require_worker(request: Request, api_key: str | None = Security(_header_scheme)) -> None:
    if not api_key:
        raise HTTPException(status_code=401, detail="X-OmniAgent-Key header missing")
    worker_secret = os.environ.get("OMNIAGENT_WORKER_SECRET", "")
    if not worker_secret or api_key != worker_secret:
        raise HTTPException(status_code=403, detail="Worker key required")
