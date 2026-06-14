"""X-OmniAgent-Key validation middleware.

Key types:
  internal  — matches OMNIAGENT_INTERNAL_KEY env var directly (CP ↔ Worker)
  api       — argon2 hash in api_keys table (services, custom UIs, bots, built-in UI)

Built-in UI key is seeded as `_built-in-ui` in api_keys on startup from OMNIAGENT_API_KEY.

Key prefix (first 8 chars) is stored alongside hash to avoid O(n) argon2 scan.
"""

import logging
import os
from typing import Literal

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from omniagent.control_plane.db import get_conn
from omniagent.control_plane.secrets import verify_key

logger = logging.getLogger(__name__)

_header_scheme = APIKeyHeader(name="X-OmniAgent-Key", auto_error=False)

KeyType = Literal["internal", "api"]


async def _resolve_key(key: str) -> KeyType:
    internal_key = os.environ.get("OMNIAGENT_INTERNAL_KEY", "")
    if internal_key and key == internal_key:
        return "internal"

    prefix = key[:8]
    async with get_conn() as conn:
        rows = await conn.execute("SELECT key_hash FROM api_keys WHERE key_prefix = %s", (prefix,))
        for row in await rows.fetchall():
            if verify_key(key, row["key_hash"]):
                return "api"

    logger.warning("auth: no matching key found (prefix=%s)", prefix)
    raise HTTPException(status_code=401, detail="Invalid X-OmniAgent-Key")


async def require_any(request: Request, api_key: str | None = Security(_header_scheme)) -> KeyType:
    key = api_key or request.query_params.get("key")
    if not key:
        raise HTTPException(status_code=401, detail="X-OmniAgent-Key header missing")
    return await _resolve_key(key)


async def require_internal(
    request: Request, api_key: str | None = Security(_header_scheme)
) -> None:
    if not api_key:
        raise HTTPException(status_code=401, detail="X-OmniAgent-Key header missing")
    internal_key = os.environ.get("OMNIAGENT_INTERNAL_KEY", "")
    if not internal_key or api_key != internal_key:
        raise HTTPException(status_code=403, detail="Internal key required")
