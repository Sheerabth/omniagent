"""OAuth2 / OIDC token acquisition with DB-backed caching."""

import logging
import time

from omniagent.db import get_conn
from omniagent.worker.http import _get_http_client

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
    return await _get_oauth_token({**security, "token_url": token_url}, auth_context)


async def _get_oauth_token(security: dict, auth_context: dict) -> str:
    # Use pre-stored token from auth code flow if present and not expired
    stored_token = auth_context.get("access_token")
    if stored_token:
        expiry = auth_context.get("token_expiry")
        if not expiry or time.time() < expiry - 30:
            return stored_token
    try:
        client_id = auth_context[security["client_id_key"]]
        client_secret = auth_context[security["client_secret_key"]]
    except KeyError as e:
        raise RuntimeError(f"auth_context missing key: {e}") from e
    cache_key = f"{security.get('token_url', '')}:{client_id}"

    async with get_conn() as conn:
        row = await (
            await conn.execute(
                "SELECT token FROM oauth_token_cache WHERE cache_key=%s AND expires_at > NOW()",
                (cache_key,),
            )
        ).fetchone()
        if row:
            return row["token"]

    refresh_token = auth_context.get(security.get("refresh_token_key", "refresh_token"))
    payload: dict = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": " ".join(security.get("scopes", [])),
    }
    if refresh_token:
        payload["grant_type"] = "refresh_token"
        payload["refresh_token"] = refresh_token
    else:
        payload["grant_type"] = "client_credentials"
    resp = await _get_http_client().post(security["token_url"], data=payload)
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Token response missing access_token (got keys: {list(data.keys())})")
    token = data["access_token"]
    expires_in = data.get("expires_in", 3600)

    async with get_conn() as conn:
        await conn.execute("DELETE FROM oauth_token_cache WHERE expires_at < NOW()")
        await conn.execute(
            """INSERT INTO oauth_token_cache (cache_key, token, expires_at)
               VALUES (%s, %s, NOW() + %s * INTERVAL '1 second')
               ON CONFLICT (cache_key) DO UPDATE SET token=EXCLUDED.token, expires_at=EXCLUDED.expires_at""",
            (cache_key, token, expires_in - 30),
        )
    return token
