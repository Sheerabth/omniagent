"""Native skill — injected by worker, not user-configurable.

Tools in the `native` namespace are added to every session's tool_snapshot at
runtime. They are handled inside the tool_exec closure before reaching the HTTP
executor, so no openapi_* fields are needed.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime


@dataclasses.dataclass
class DeferInfo:
    delay_seconds: int = 0
    resume_at: datetime | None = None  # if set, takes precedence over delay_seconds

    def scheduled_at(self) -> datetime:
        if self.resume_at:
            return self.resume_at
        from datetime import timedelta

        return datetime.now(UTC) + timedelta(seconds=self.delay_seconds)


# Descriptions injected into system prompt and tool listings
NATIVE_TOOL_DESCRIPTIONS: dict[str, str] = {
    "native.memory_get": "Read a value from agent memory. Returns null if key doesn't exist.",
    "native.memory_set": "Write a value to agent memory. Persists across sessions.",
    "native.memory_delete": "Delete a key from agent memory.",
    "native.memory_list": "List all keys in agent memory.",
    "native.schedule_list": "List all schedules for THIS agent. Use this to check if a schedule already exists before creating one.",
    "native.schedule_create": (
        "Create a recurring scheduled run for THIS agent on a cron schedule. "
        "Returns the new schedule ID and next_run_at."
    ),
    "native.defer_turn": (
        "Pause this session and resume after delay_seconds. "
        "Use when you have a duration to wait (e.g. 'check again in 30 seconds'). "
        "After calling, write a brief status — the next turn receives a [RESUME] message."
    ),
    "native.defer_turn_until": (
        "Pause this session and resume at a specific UTC timestamp. "
        "Use when you have an absolute time to wait until (e.g. ready_at, alert_at, scheduled_at fields). "
        "Prefer this over defer_turn when the API gives you an ISO timestamp. "
        "After calling, write a brief status — the next turn receives a [RESUME] message."
    ),
}

# Input schemas for each native tool (used by adapters to expose the tools to the LLM)
NATIVE_TOOL_SCHEMAS: dict[str, dict] = {
    "native.memory_get": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    },
    "native.memory_set": {
        "type": "object",
        "properties": {
            "key": {"type": "string"},
            "value": {"description": "Any JSON-serializable value"},
        },
        "required": ["key", "value"],
    },
    "native.memory_delete": {
        "type": "object",
        "properties": {"key": {"type": "string"}},
        "required": ["key"],
    },
    "native.memory_list": {
        "type": "object",
        "properties": {},
    },
    "native.schedule_list": {
        "type": "object",
        "properties": {},
    },
    "native.schedule_create": {
        "type": "object",
        "properties": {
            "cron_expr": {
                "type": "string",
                "description": "Cron expression, e.g. '0 9 * * *' for daily at 9am UTC, '*/2 * * * *' for every 2 minutes.",
            },
            "prompt": {
                "type": "string",
                "description": "Prompt this agent will receive on each scheduled run.",
            },
            "llm_context": {
                "type": "object",
                "description": "Optional context passed to the LLM on each run.",
            },
        },
        "required": ["cron_expr", "prompt"],
    },
    "native.defer_turn": {
        "type": "object",
        "properties": {
            "delay_seconds": {
                "type": "integer",
                "description": "Seconds before resuming. 0 = defer immediately after this turn.",
                "default": 0,
            }
        },
    },
    "native.defer_turn_until": {
        "type": "object",
        "properties": {
            "iso_timestamp": {
                "type": "string",
                "description": "UTC ISO 8601 timestamp to resume at, e.g. 2026-06-20T15:30:00Z",
            }
        },
        "required": ["iso_timestamp"],
    },
}
