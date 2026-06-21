"""OAuth2 authorization code connect flow."""

import json
import secrets
import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from omniagent.api.crypto import decrypt_auth_context, encrypt_auth_context
from omniagent.api.db import get_conn

router = APIRouter(prefix="/oauth2", tags=["oauth2"])


@router.get("/connect")
async def oauth2_connect(
    namespace: str,
    tool_name: str,
    request: Request,
) -> RedirectResponse:
    async with get_conn() as conn:
        auth_row = await (
            await conn.execute(
                "SELECT auth_context FROM namespace_auth WHERE namespace=%s", (namespace,)
            )
        ).fetchone()
        auth_ctx: dict = decrypt_auth_context(auth_row["auth_context"]) if auth_row else {}

        tool_row = await (
            await conn.execute("SELECT openapi_security FROM tools WHERE name=%s", (tool_name,))
        ).fetchone()
        if not tool_row:
            raise HTTPException(404, "Tool not found")
        sec: dict = tool_row["openapi_security"] or {}

        if sec.get("type") != "oauth2" or not sec.get("authorization_url"):
            raise HTTPException(400, "Tool has no OAuth2 authorization code flow")

        client_id_key = sec.get("client_id_key", "client_id")
        client_id = auth_ctx.get(client_id_key)
        if not client_id:
            raise HTTPException(
                400,
                f"Namespace auth missing '{client_id_key}' — set client_id and client_secret before connecting",
            )

        state = secrets.token_urlsafe(16)
        redirect_uri = str(request.base_url).rstrip("/") + "/oauth2/callback"
        expires_at = datetime.now(UTC) + timedelta(minutes=10)
        await conn.execute("DELETE FROM oauth2_pending WHERE expires_at < NOW()")
        await conn.execute(
            "INSERT INTO oauth2_pending (state, data, expires_at) VALUES (%s, %s, %s)",
            (
                state,
                json.dumps(
                    {
                        "namespace": namespace,
                        "security": sec,
                        "auth_ctx": auth_ctx,
                        "redirect_uri": redirect_uri,
                    }
                ),
                expires_at,
            ),
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
        row = await (
            await conn.execute(
                "DELETE FROM oauth2_pending WHERE state=%s AND expires_at > NOW() RETURNING data",
                (state,),
            )
        ).fetchone()
    if not row:
        raise HTTPException(400, "Invalid or expired OAuth2 state")
    entry = row["data"]

    sec = entry["security"]
    auth_ctx = entry["auth_ctx"]
    client_id = auth_ctx.get(sec.get("client_id_key", "client_id"), "")
    client_secret = auth_ctx.get(sec.get("client_secret_key", "client_secret"), "")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                sec["token_url"],
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": entry["redirect_uri"],
                    "client_id": client_id,
                    "client_secret": client_secret,
                },
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Provider token exchange failed: {e.response.status_code}") from e

    if "access_token" not in data:
        raise HTTPException(502, f"Provider returned no access_token: {data}")

    new_ctx = {
        **(auth_ctx or {}),
        "access_token": data["access_token"],
    }
    if "refresh_token" in data:
        new_ctx["refresh_token"] = data["refresh_token"]
    if "expires_in" in data:
        new_ctx["token_expiry"] = int(datetime.now(UTC).timestamp()) + int(data["expires_in"])

    async with get_conn() as conn:
        await conn.execute(
            """INSERT INTO namespace_auth (namespace, auth_context, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (namespace) DO UPDATE
                 SET auth_context = EXCLUDED.auth_context, updated_at = NOW()""",
            (entry["namespace"], encrypt_auth_context(new_ctx)),
        )

    return RedirectResponse("/?oauth2=success")
