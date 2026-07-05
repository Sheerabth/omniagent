"""X-OmniAgent-Key validation.

Key type: api — argon2 hash in api_keys table (services, custom UIs, bots)

Key prefix (first 8 chars) is stored alongside hash to avoid O(n) argon2 scan.

Scopes: each api key has a list of scopes. `admin` is a wildcard for all scopes.
  tools:read, tools:write, toolboxes:read, toolboxes:write,
  auth:read, auth:write, agents:read, agents:write,
  sessions:read, sessions:write, keys:manage
"""

import logging
from typing import Protocol

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from omniagent.api.queries import select_key_by_prefix
from omniagent.api.secrets import verify_key
from omniagent.constants import X_OMNIAGENT_KEY
from omniagent.db import get_conn

logger = logging.getLogger(__name__)

ADMIN_SCOPE = "admin"

_header_scheme = APIKeyHeader(name=X_OMNIAGENT_KEY, auto_error=False)


async def _resolve_key(key: str) -> list[str]:
    prefix = key[:8]
    async with get_conn() as conn:
        rows = await conn.execute(
            select_key_by_prefix,
            {"prefix": prefix},
        )
        for row in rows.mappings().fetchall():
            if verify_key(key, row["key_hash"]):
                return list(row["scopes"] or [ADMIN_SCOPE])

    logger.warning("auth: no matching key found (prefix=%s)", prefix)
    raise HTTPException(status_code=401, detail=f"Invalid {X_OMNIAGENT_KEY}")


async def _resolve_request(request: Request, api_key: str | None) -> list[str]:
    from omniagent.api.routes.auth import validate_session

    if validate_session(request):
        return [ADMIN_SCOPE]
    if not api_key:
        raise HTTPException(status_code=401, detail=f"{X_OMNIAGENT_KEY} header missing")
    return await _resolve_key(api_key)


async def require_any(request: Request, api_key: str | None = Security(_header_scheme)) -> None:
    await _resolve_request(request, api_key)


class _ScopeChecker(Protocol):
    """FastAPI dependency — checks the request's API key has *scope*."""

    async def __call__(self, request: Request, api_key: str | None) -> None: ...


def require_scope(scope: str) -> _ScopeChecker:
    async def check(request: Request, api_key: str | None = Security(_header_scheme)) -> None:
        scopes = await _resolve_request(request, api_key)
        if ADMIN_SCOPE in scopes or scope in scopes:
            return
        raise HTTPException(status_code=403, detail=f"Key missing scope: {scope}")

    return check
