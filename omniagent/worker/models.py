"""Pydantic models for the worker layer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class ToolSnapshot(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    openapi_method: str
    openapi_path: str
    openapi_base_url: str
    openapi_security: dict | None = None
    skill_name: str = ""


class SkillSnapshot(BaseModel):
    system_prompt: str = ""
    instructions: str = ""
    skill_context: Any = None


class SessionConfig(BaseModel):
    agent_name: str
    harness: str
    model: str
    system_prompt: str
    use_monty: bool
    auth_context: Any = None
    skills: list[SkillSnapshot]
    tool_snapshot: dict[str, ToolSnapshot]


class BaseEvent(BaseModel):
    type: str


class ToolCallEvent(BaseEvent):
    type: str = "tool_call"
    tool: str
    input: dict[str, Any]
    harness: str | None = None
    skill_name: str | None = None


class ToolResultEvent(BaseEvent):
    type: str = "tool_result"
    tool: str
    input: dict[str, Any]
    success: bool
    output: Any = None
    harness: str | None = None
    error: str | None = None
    skill_name: str | None = None


class SystemPromptEvent(BaseEvent):
    type: str = "system_prompt"
    content: str
    input: list


class ThinkingEvent(BaseEvent):
    type: str = "thinking"
    content: str


class ErrorEvent(BaseEvent):
    type: str = "error"
    reason: str
