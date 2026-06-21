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


# ── Toolboxes ──────────────────────────────────────────────────────────────


class ToolboxCreate(BaseModel):
    name: str
    version: str
    tool_names: list[str]
    system_prompt: str = ""


class ToolboxRecord(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    tool_names: list[str]
    system_prompt: str
    created_at: datetime
    updated_at: datetime


class NamespaceAuthSet(BaseModel):
    auth_context: Any


class NamespaceRecord(BaseModel):
    namespace: str
    tool_count: int
    auth_context_keys: list[str] = []


# ── Agents ─────────────────────────────────────────────────────────────────


class AgentCreate(BaseModel):
    name: str
    version: str
    harness: str
    model: str = ""
    toolbox_refs: dict[str, str] = {}  # {"toolbox_name": "toolbox_version"}
    tool_refs: list[str] = []  # directly-attached tool names (no toolbox required)
    system_prompt: str = ""
    use_monty: bool = False


class AgentRecord(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    harness: str
    model: str
    toolbox_refs: dict[str, str]
    tool_refs: list[str] = []
    system_prompt: str
    use_monty: bool
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
    toolbox_versions: dict[str, str]
    tool_refs: list[str] = []
    status: str
    schedule_id: uuid.UUID | None = None
    is_scheduled: bool = False
    created_at: datetime


class RunRequest(BaseModel):
    prompt: str


class ResumeRequest(BaseModel):
    message: str = ""  # injected as [RESUME: <message>] user turn


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
    toolbox_versions: dict[str, str]
    tool_refs: list[str] = []


# ── Settings ───────────────────────────────────────────────────────────────


VALID_SCOPES = {
    "admin",
    "tools:read",
    "tools:write",
    "toolboxes:read",
    "toolboxes:write",
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


# ── Schedules ──────────────────────────────────────────────────────────────


class ScheduleCreate(BaseModel):
    agent_name: str
    cron_expr: str
    prompt: str
    auth_context: Any = None
    enabled: bool = True


class ScheduleUpdate(BaseModel):
    cron_expr: str | None = None
    prompt: str | None = None
    enabled: bool | None = None


class ScheduleRecord(BaseModel):
    id: uuid.UUID
    agent_name: str
    cron_expr: str
    prompt: str
    enabled: bool
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
