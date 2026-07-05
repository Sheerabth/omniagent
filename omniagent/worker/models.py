"""Pydantic models for the worker layer."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel

from omniagent.constants import EventType


class ToolSnapshot(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    openapi_method: str = ""
    openapi_path: str = ""
    openapi_base_url: str = ""
    openapi_security: dict | None = None
    timeout: int | None = None
    skill_name: str = ""
    auth_context: Any = None  # decrypted JSON blob, subscripted dynamically by security scheme
    is_native: bool = False  # native tools are handled before the HTTP executor


class ToolboxSnapshot(BaseModel):
    system_prompt: str = ""


class SessionConfig(BaseModel):
    agent_name: str
    harness: str
    model: str
    system_prompt: str
    use_monty: bool
    toolboxes: list[ToolboxSnapshot]
    tool_snapshot: dict[str, ToolSnapshot]


class BaseEvent(BaseModel):
    type: str


class ToolCallEvent(BaseEvent):
    type: str = EventType.TOOL_CALL
    tool: str
    input: dict[str, Any]
    harness: str | None = None
    skill_name: str | None = None


class ToolResultEvent(BaseEvent):
    type: str = EventType.TOOL_RESULT
    tool: str
    input: dict[str, Any]
    success: bool
    output: Any = None
    harness: str | None = None
    error: str | None = None
    skill_name: str | None = None


class SystemPromptEvent(BaseEvent):
    type: str = EventType.SYSTEM_PROMPT
    content: str
    input: list


class ThinkingEvent(BaseEvent):
    type: str = EventType.THINKING
    content: str


class ErrorEvent(BaseEvent):
    type: str = EventType.ERROR
    reason: str


# ── Behavioural Protocols ─────────────────────────────────────────────────


class ToolExecutor(Protocol):
    async def __call__(self, tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]: ...


class EventEmitter(Protocol):
    async def __call__(self, event: BaseEvent) -> None: ...


class MontyExecutor(Protocol):
    async def __call__(self, code: str, observation: str) -> str: ...


# ── Database row models ────────────────────────────────────────────────────
# Internal: validate DB rows at the boundary so downstream code gets typed
# fields instead of dict[str, Any].


class _SessionConfigRow(BaseModel):
    """Partial sessions row for ``_fetch_session_config`` — only the columns
    needed to load agent/toolbox/tool refs, avoids fetching full messages."""

    agent_name: str
    agent_version: str
    toolbox_versions: dict[str, str]
    tool_refs: list[str]


class _NamespaceAuthRow(BaseModel):
    """Raw ``namespace_auth`` table row.  ``auth_context`` is the Fernet-
    encrypted blob; call ``decrypt_auth_context`` to unwrap it."""

    namespace: str
    scheme_name: str
    auth_context: str | None
