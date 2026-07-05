"""Shared constants and enums — usable by both api/ and worker/ without cross-module imports."""

import uuid
from enum import StrEnum


class SessionStatus(StrEnum):
    """Session lifecycle states stored in ``sessions.status`` column."""

    PENDING = "pending"
    RUNNING = "running"
    IDLE = "idle"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEFERRED = "deferred"


class NotifyType(StrEnum):
    """Payload types sent via ``pg_notify(ch, payload)``."""

    CANCELLED = "cancelled"
    COMPLETE = "complete"
    CANCELLING = "cancelling"
    ERROR = "error"
    UPDATE = "update"


class EventType(StrEnum):
    """Internal worker event routing types (``BaseEvent.type`` field)."""

    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    SYSTEM_PROMPT = "system_prompt"
    THINKING = "thinking"
    ERROR = "error"
    RUNNING = "running"
    DEFERRED = "deferred"


class HarnessName(StrEnum):
    """Valid agent harness identifiers."""

    CLAUDE = "claude"
    ANTIGRAVITY = "antigravity"


def session_channel(session_id: str | uuid.UUID) -> str:
    """Convert session UUID to LISTEN channel name.

    ``pg_notify`` and ``sse_hub.subscribe`` both use this convention.
    """
    return "session_" + str(session_id).replace("-", "_")


# ── HTTP headers ──────────────────────────────────────────────────────────
AUTH_HEADER = "Authorization"
CONTENT_TYPE = "Content-Type"
APPLICATION_JSON = "application/json"
X_OMNIAGENT_KEY = "X-OmniAgent-Key"
X_TRACE_ID = "X-Trace-Id"
COOKIE = "Cookie"

# ── Security types (shared between openapi.py parsing and tools.py execution)
SEC_TYPE_BEARER = "bearer"
SEC_TYPE_BASIC = "basic"
SEC_TYPE_API_KEY = "apiKey"
SEC_TYPE_OAUTH2 = "oauth2"
SEC_TYPE_OIDC = "oidc"

# ── OAuth grant types ─────────────────────────────────────────────────────
GRANT_TYPE_AUTHORIZATION_CODE = "authorization_code"
GRANT_TYPE_CLIENT_CREDENTIALS = "client_credentials"
GRANT_TYPE_REFRESH_TOKEN = "refresh_token"

# ── OAuth token keys ──────────────────────────────────────────────────────
TOKEN_KEY_ACCESS_TOKEN = "access_token"
TOKEN_KEY_REFRESH_TOKEN = "refresh_token"
TOKEN_KEY_EXPIRES_IN = "expires_in"
TOKEN_KEY_CLIENT_ID = "client_id"
TOKEN_KEY_CLIENT_SECRET = "client_secret"
TOKEN_KEY_SCOPES = "scopes"
TOKEN_KEY_TOKEN_URL = "token_url"
TOKEN_KEY_GRANT_TYPE = "grant_type"


# ── JSONB deserialization ─────────────────────────────────────────────────


def parse_jsonb(v: object) -> object:
    """Parse JSONB values that SQLAlchemy Core returns as raw strings.

    Use as a Pydantic ``field_validator(mode="before")`` on dict/list fields
    mapped to PostgreSQL JSONB columns.
    """
    if isinstance(v, str) and v and v[0] in ("{", "["):
        import json

        return json.loads(v)
    return v
