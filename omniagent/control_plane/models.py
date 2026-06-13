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
    available: bool


# ── Skills ─────────────────────────────────────────────────────────────────

class SkillCreate(BaseModel):
    name: str
    tool_names: list[str]
    instructions: str = ""
    system_prompt: str = ""


class SkillPatch(BaseModel):
    tool_names: list[str] | None = None
    instructions: str | None = None
    system_prompt: str | None = None


class SkillRecord(BaseModel):
    id: uuid.UUID
    name: str
    tool_names: list[str]
    instructions: str
    system_prompt: str
    created_at: datetime
    updated_at: datetime


# ── Agents ─────────────────────────────────────────────────────────────────

class AgentCreate(BaseModel):
    name: str
    harness: str
    skill_names: list[str] = []
    system_prompt: str = ""
    use_monty: bool = False


class AgentPatch(BaseModel):
    skill_names: list[str] | None = None
    harness: str | None = None
    system_prompt: str | None = None
    use_monty: bool | None = None


class AgentRecord(BaseModel):
    id: uuid.UUID
    name: str
    harness: str
    skill_names: list[str]
    system_prompt: str
    use_monty: bool
    created_at: datetime
    updated_at: datetime


# ── Sessions ───────────────────────────────────────────────────────────────

class SessionCreate(BaseModel):
    agent_id: uuid.UUID


class SessionRecord(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
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


# ── Settings ───────────────────────────────────────────────────────────────

class ClientKeyCreate(BaseModel):
    name: str


class ClientKeyRecord(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class ServiceKeyCreate(BaseModel):
    name: str


class ServiceKeyRecord(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime


class LlmKeyCreate(BaseModel):
    harness: str
    api_key: str


class LlmKeyRecord(BaseModel):
    harness: str
    key_hint: str


# ── Internal ───────────────────────────────────────────────────────────────

class ToolExecuteRequest(BaseModel):
    tool_name: str
    input: dict[str, Any]
    session_id: uuid.UUID


class ToolExecuteResponse(BaseModel):
    output: dict[str, Any]


class SessionResultRequest(BaseModel):
    result: str


class SessionEventRequest(BaseModel):
    type: str
    content: str | None = None
    tool: str | None = None
    input: dict[str, Any] | None = None
    success: bool | None = None
    reason: str | None = None
