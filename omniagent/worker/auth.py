"""OAuth2 / OIDC token acquisition with DB-backed caching."""

import logging
import time

from omniagent.config import settings
from omniagent.constants import (
    GRANT_TYPE_CLIENT_CREDENTIALS,
    GRANT_TYPE_REFRESH_TOKEN,
    TOKEN_KEY_ACCESS_TOKEN,
    TOKEN_KEY_CLIENT_ID,
    TOKEN_KEY_CLIENT_SECRET,
    TOKEN_KEY_EXPIRES_IN,
    TOKEN_KEY_GRANT_TYPE,
    TOKEN_KEY_REFRESH_TOKEN,
    TOKEN_KEY_SCOPES,
    TOKEN_KEY_TOKEN_URL,
)
from omniagent.db import get_conn
from omniagent.worker.http import _get_http_client
from omniagent.worker.queries import (
    delete_expired_oauth_tokens,
    select_oauth_token_valid,
    upsert_oauth_token,
)

logger = logging.getLogger(__name__)

_oidc_discovery_cache: dict[str, str] = {}  # ponytail: process-local, discovery docs don't change


async def _get_oidc_token(security: dict, auth_context: dict) -> str:
    discovery_url = security["openid_connect_url"]
    if discovery_url not in _oidc_discovery_cache:
        resp = await _get_http_client().get(discovery_url, timeout=10)
        resp.raise_for_status()
        doc = resp.json()
        if "token_endpoint" not in doc:
            raise RuntimeError(f"OIDC discovery at {discovery_url} missing token_endpoint")
        _oidc_discovery_cache[discovery_url] = doc["token_endpoint"]
    token_url = _oidc_discovery_cache[discovery_url]
    return await _get_oauth_token({**security, TOKEN_KEY_TOKEN_URL: token_url}, auth_context)


async def _get_oauth_token(security: dict, auth_context: dict) -> str:
    # Use pre-stored token from auth code flow if present and not expired
    stored_token = auth_context.get(TOKEN_KEY_ACCESS_TOKEN)
    if stored_token:
        expiry = auth_context.get("token_expiry")
        if not expiry or time.time() < expiry - settings.token_expiry_buffer_seconds:
            return stored_token
    try:
        client_id = auth_context[security[TOKEN_KEY_CLIENT_ID]]
        client_secret = auth_context[security[TOKEN_KEY_CLIENT_SECRET]]
    except KeyError as e:
        raise RuntimeError(f"auth_context missing key: {e}") from e
    cache_key = f"{security.get(TOKEN_KEY_TOKEN_URL, '')}:{client_id}"

    async with get_conn() as conn:
        result = await conn.execute(
            select_oauth_token_valid,
            {"cache_key": cache_key},
        )
        row = result.mappings().fetchone()
        if row:
            return row["token"]

    refresh_token = auth_context.get(security.get("refresh_token_key", TOKEN_KEY_REFRESH_TOKEN))
    payload: dict = {
        TOKEN_KEY_CLIENT_ID: client_id,
        TOKEN_KEY_CLIENT_SECRET: client_secret,
        TOKEN_KEY_SCOPES: " ".join(security.get(TOKEN_KEY_SCOPES, [])),
    }
    if refresh_token:
        payload[TOKEN_KEY_GRANT_TYPE] = GRANT_TYPE_REFRESH_TOKEN
        payload[TOKEN_KEY_REFRESH_TOKEN] = refresh_token
    else:
        payload[TOKEN_KEY_GRANT_TYPE] = GRANT_TYPE_CLIENT_CREDENTIALS
    resp = await _get_http_client().post(security[TOKEN_KEY_TOKEN_URL], data=payload)
    resp.raise_for_status()
    data = resp.json()
    if TOKEN_KEY_ACCESS_TOKEN not in data:
        raise RuntimeError(f"Token response missing access_token (got keys: {list(data.keys())})")
    token = data[TOKEN_KEY_ACCESS_TOKEN]
    expires_in = data.get(TOKEN_KEY_EXPIRES_IN, 3600)

    async with get_conn() as conn:
        await conn.execute(delete_expired_oauth_tokens)
        await conn.execute(
            upsert_oauth_token,
            {
                "cache_key": cache_key,
                "token": token,
                "ttl": expires_in - settings.token_expiry_buffer_seconds,
            },
        )
    return token
