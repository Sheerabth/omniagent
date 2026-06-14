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
    service: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execute_url: str = ""


# ── Skills ─────────────────────────────────────────────────────────────────


class SkillCreate(BaseModel):
    name: str
    version: str
    tool_names: list[str]
    instructions: str = ""
    system_prompt: str = ""


class SkillRecord(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    tool_names: list[str]
    instructions: str
    system_prompt: str
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


class AgentRecord(BaseModel):
    id: uuid.UUID
    name: str
    version: str
    harness: str
    model: str
    skill_refs: dict[str, str]
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
    skill_versions: dict[str, str]
    status: str
    created_at: datetime


class RunRequest(BaseModel):
    prompt: str


class ToolCallEntry(BaseModel):
    tool_name: str
    input: dict[str, Any]
    output: dict[str, Any]
    harness: str
    timestamp: datetime
    success: bool
    error: str | None = None


class SessionStatus(BaseModel):
    status: str
    result: str | None
    messages: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    agent_name: str
    agent_version: str
    skill_versions: dict[str, str]


# ── Settings ───────────────────────────────────────────────────────────────


class ServiceKeyCreate(BaseModel):
    name: str


class ServiceKeyRecord(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


# ── Internal ───────────────────────────────────────────────────────────────


class SessionResultRequest(BaseModel):
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
