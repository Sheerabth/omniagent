"""Pydantic request/response models for the control plane API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

# ── Tools ──────────────────────────────────────────────────────────────────


class ToolRecord(BaseModel):
    name: str
    namespace: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    openapi_method: str
    openapi_path: str
    openapi_base_url: str
    openapi_security: dict | None = None
    timeout: int | None = None


# ── Skills ─────────────────────────────────────────────────────────────────


class SkillCreate(BaseModel):
    name: str
    version: str
    tool_names: list[str]
    instructions: str = ""
    system_prompt: str = ""
    skill_context: Any = None


class SkillRecord(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    tool_names: list[str]
    instructions: str
    system_prompt: str
    skill_context: Any = None
    created_at: datetime
    updated_at: datetime


# ── Agents ─────────────────────────────────────────────────────────────────


class AgentCreate(BaseModel):
    name: str
    version: str
    harness: str
    model: str = ""
    skill_refs: dict[str, str] = {}  # {"skill_name": "skill_version"}
    system_prompt: str = ""
    use_monty: bool = False
    auth_context: Any = None


class AgentRecord(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    harness: str
    model: str
    skill_refs: dict[str, str]
    system_prompt: str
    use_monty: bool
    auth_context_keys: list[str] = []
    created_at: datetime
    updated_at: datetime


# ── Sessions ───────────────────────────────────────────────────────────────


class SessionCreate(BaseModel):
    agent_name: str
    agent_version: str | None = None  # defaults to latest (most recently created)


class SessionRecord(BaseModel):
    id: uuid.UUID
    agent_name: str
    agent_version: str
    skill_versions: dict[str, str]
    status: str
    created_at: datetime


class RunRequest(BaseModel):
    prompt: str
    auth_context: Any = None
    llm_context: Any = None


class MessageRecord(BaseModel):
    role: str
    content: str
    timestamp: str


class ToolCallEntry(BaseModel):
    tool_name: str
    input: dict[str, Any]
    output: Any
    harness: str | None = None
    skill_name: str | None = None
    timestamp: datetime
    success: bool
    error: str | None = None


class SessionStatus(BaseModel):
    status: str
    result: str | None
    messages: list[MessageRecord]
    tool_calls: list[ToolCallEntry]
    agent_name: str
    agent_version: str
    skill_versions: dict[str, str]


# ── Settings ───────────────────────────────────────────────────────────────


VALID_SCOPES = {
    "admin",
    "tools:read",
    "tools:write",
    "skills:read",
    "skills:write",
    "agents:read",
    "agents:write",
    "sessions:read",
    "sessions:write",
    "keys:manage",
}


class ApiKeyCreate(BaseModel):
    name: str
    scopes: list[str] = ["admin"]


class ApiKeyRecord(BaseModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    created_at: datetime


class ApiKeyResponse(BaseModel):
    id: uuid.UUID
    name: str
    scopes: list[str]
    created_at: datetime
    key: str


# ── Internal ───────────────────────────────────────────────────────────────


class SessionResultRequest(BaseModel):
    result: str


class SessionCompletePayload(BaseModel):
    type: str = "complete"
    result: str


class SessionEventRequest(BaseModel):
    type: str
    content: str | None = None
    tool: str | None = None
    input: Any = None
    output: Any = None
    success: bool | None = None
    reason: str | None = None
    harness: str | None = None
    error: str | None = None
    skill_name: str | None = None
