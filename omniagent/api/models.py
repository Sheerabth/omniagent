"""Pydantic request/response models for the control plane API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, field_validator

from omniagent.constants import NotifyType, parse_jsonb

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

    _parse_input_schema = field_validator("input_schema", mode="before")(parse_jsonb)
    _parse_output_schema = field_validator("output_schema", mode="before")(parse_jsonb)
    _parse_openapi_security = field_validator("openapi_security", mode="before")(parse_jsonb)


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


class SchemeRecord(BaseModel):
    scheme_name: str
    auth_context_keys: list[str] = []


class NamespaceRecord(BaseModel):
    namespace: str
    tool_count: int
    schemes: list[SchemeRecord] = []


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

    _parse_toolbox_refs = field_validator("toolbox_refs", mode="before")(parse_jsonb)


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

    _parse_toolbox_versions = field_validator("toolbox_versions", mode="before")(parse_jsonb)


class FileRef(BaseModel):
    """Lightweight file metadata — points into MinIO session storage."""

    path: str  # relative within session, e.g. "report.pdf"
    name: str  # filename
    content_type: str  # MIME type
    size: int  # bytes
    updated_at: str  # ISO timestamp


class RunRequest(BaseModel):
    prompt: str
    files: list[str] = []  # paths of pre-uploaded files to attach to this turn


class ResumeRequest(BaseModel):
    message: str = ""  # injected as [RESUME: <message>] user turn
    files: list[str] = []  # paths of pre-uploaded files to attach


class MessageRecord(BaseModel):
    role: str
    content: str
    files: list[FileRef] = []  # files attached when this message was sent
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

    _parse_input = field_validator("input", mode="before")(parse_jsonb)


class SessionStatus(BaseModel):
    status: str
    result: str | None
    messages: list[MessageRecord]
    tool_calls: list[ToolCallEntry]
    agent_name: str
    agent_version: str
    toolbox_versions: dict[str, str]
    tool_refs: list[str] = []

    _parse_toolbox_versions = field_validator("toolbox_versions", mode="before")(parse_jsonb)


# ── Settings ───────────────────────────────────────────────────────────────


VALID_SCOPES = {
    "admin",
    "tools:read",
    "tools:write",
    "toolboxes:read",
    "toolboxes:write",
    "auth:read",
    "auth:write",
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
    type: str = NotifyType.COMPLETE
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


# ── Memory ──────────────────────────────────────────────────────────────────


class MemorySetRequest(BaseModel):
    value: Any


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
