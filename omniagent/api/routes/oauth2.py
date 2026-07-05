"""OAuth2 authorization code connect flow."""

import json
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from omniagent.api.auth import require_scope
from omniagent.api.queries import (
    delete_expired_oauth_pending,
    delete_oauth2_pending_returning,
    insert_oauth2_pending,
    select_namespace_auth_context,
    select_tool_security,
    upsert_oauth2_namespace_auth,
)
from omniagent.config import settings
from omniagent.constants import (
    APPLICATION_JSON,
    GRANT_TYPE_AUTHORIZATION_CODE,
    SEC_TYPE_OAUTH2,
    TOKEN_KEY_ACCESS_TOKEN,
    TOKEN_KEY_CLIENT_ID,
    TOKEN_KEY_CLIENT_SECRET,
    TOKEN_KEY_EXPIRES_IN,
    TOKEN_KEY_REFRESH_TOKEN,
)
from omniagent.crypto import decrypt_auth_context, encrypt_auth_context
from omniagent.db import get_conn

router = APIRouter(prefix="/oauth2", tags=["oauth2"])


@router.get("/connect")
async def oauth2_connect(
    namespace: str,
    tool_name: str,
    request: Request,
    _=Depends(require_scope("auth:write")),
) -> RedirectResponse:
    async with get_conn() as conn:
        tool_row = (
            (await conn.execute(select_tool_security, {"tool_name": tool_name}))
            .mappings()
            .fetchone()
        )
        if not tool_row:
            raise HTTPException(404, "Tool not found")
        sec: dict = tool_row["openapi_security"] or {}

        if sec.get("type") != SEC_TYPE_OAUTH2 or not sec.get("authorization_url"):
            raise HTTPException(400, "Tool has no OAuth2 authorization code flow")

        scheme_name: str = sec.get("scheme_name", "")
        auth_row = (
            (
                await conn.execute(
                    select_namespace_auth_context,
                    {"namespace": namespace, "scheme_name": scheme_name},
                )
            )
            .mappings()
            .fetchone()
        )
        auth_ctx: dict = decrypt_auth_context(auth_row["auth_context"]) if auth_row else {}

        client_id_key = sec.get("client_id_key", TOKEN_KEY_CLIENT_ID)
        client_id = auth_ctx.get(client_id_key)
        if not client_id:
            raise HTTPException(
                400,
                f"Namespace auth missing '{client_id_key}' — set client_id and client_secret before connecting",
            )

        state = secrets.token_urlsafe(16)
        redirect_uri = str(request.base_url).rstrip("/") + "/oauth2/callback"
        expires_at = datetime.now(UTC) + timedelta(minutes=settings.oauth_pending_expiry_minutes)
        await conn.execute(delete_expired_oauth_pending)
        await conn.execute(
            insert_oauth2_pending,
            {
                "state": state,
                "data": json.dumps(
                    {
                        "namespace": namespace,
                        "scheme_name": scheme_name,
                        "security": sec,
                        "auth_ctx": auth_ctx,
                        "redirect_uri": redirect_uri,
                    }
                ),
                "expires_at": expires_at,
            },
        )

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": " ".join(sec.get("scopes", [])),
        "state": state,
    }
    auth_url = sec["authorization_url"] + "?" + urllib.parse.urlencode(params)
    return RedirectResponse(auth_url)


@router.get("/callback")
async def oauth2_callback(code: str, state: str) -> RedirectResponse:
    async with get_conn() as conn:
        row = (
            (await conn.execute(delete_oauth2_pending_returning, {"state": state}))
            .mappings()
            .fetchone()
        )
    if not row:
        raise HTTPException(400, "Invalid or expired OAuth2 state")
    entry = row["data"]

    sec = entry["security"]
    auth_ctx = entry["auth_ctx"]
    client_id = auth_ctx.get(sec.get("client_id_key", TOKEN_KEY_CLIENT_ID), "")
    client_secret = auth_ctx.get(sec.get("client_secret_key", TOKEN_KEY_CLIENT_SECRET), "")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                sec["token_url"],
                data={
                    "grant_type": GRANT_TYPE_AUTHORIZATION_CODE,
                    "code": code,
                    "redirect_uri": entry["redirect_uri"],
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": APPLICATION_JSON},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Provider token exchange failed: {e.response.status_code}") from e

    if TOKEN_KEY_ACCESS_TOKEN not in data:
        raise HTTPException(502, f"Provider returned no access_token: {data}")

    new_ctx = {
        **(auth_ctx or {}),
        TOKEN_KEY_ACCESS_TOKEN: data[TOKEN_KEY_ACCESS_TOKEN],
    }
    if TOKEN_KEY_REFRESH_TOKEN in data:
        new_ctx[TOKEN_KEY_REFRESH_TOKEN] = data[TOKEN_KEY_REFRESH_TOKEN]
    if TOKEN_KEY_EXPIRES_IN in data:
        new_ctx["token_expiry"] = int(datetime.now(UTC).timestamp()) + int(
            data[TOKEN_KEY_EXPIRES_IN]
        )

    async with get_conn() as conn:
        await conn.execute(
            upsert_oauth2_namespace_auth,
            {
                "namespace": entry["namespace"],
                "scheme_name": entry["scheme_name"],
                "auth_context": encrypt_auth_context(new_ctx),
            },
        )

    return RedirectResponse("/?oauth2=success")
