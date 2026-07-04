"""UI admin login — HMAC-signed stateless session cookie."""

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from omniagent.config import settings

router = APIRouter(tags=["auth"])

_SESSION_TTL = settings.ui_session_hours * 3600
_COOKIE = "omniagent_session"


def _sign(payload: dict) -> str:
    password = settings.ui_password
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(password.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify(token: str) -> dict | None:
    try:
        body, sig = token.rsplit(".", 1)
    except ValueError:
        return None
    password = settings.ui_password
    expected = hmac.new(password.encode(), body.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return None
    payload = json.loads(base64.urlsafe_b64decode(body))
    if time.time() > payload.get("exp", 0):
        return None
    return payload


def validate_session(request: Request) -> bool:
    token = request.cookies.get(_COOKIE)
    return bool(token and _verify(token))


class LoginRequest(BaseModel):
    password: str


@router.post("/auth/login", include_in_schema=False)
async def login(body: LoginRequest, response: Response) -> dict:
    password = settings.ui_password or None
    if not password:
        raise HTTPException(500, "UI_PASSWORD not configured")
    if not secrets.compare_digest(body.password, password):
        raise HTTPException(401, "Invalid password")
    token = _sign({"exp": int(time.time()) + _SESSION_TTL, "jti": secrets.token_hex(8)})
    response.set_cookie(_COOKIE, token, httponly=True, samesite="strict", max_age=_SESSION_TTL)
    return {"ok": True}


@router.post("/auth/logout", include_in_schema=False)
async def logout(response: Response) -> dict:
    response.delete_cookie(_COOKIE)
    return {"ok": True}
