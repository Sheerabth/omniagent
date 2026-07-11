"""Native skill — injected by worker, not user-configurable.

Tools in the `native` namespace are added to every session's tool_snapshot at
runtime. They are handled inside the tool_exec closure before reaching the HTTP
executor, so no openapi_* fields are needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel


class DeferInfo(BaseModel):
    delay_seconds: int = 0
    resume_at: datetime | None = None  # if set, takes precedence over delay_seconds

    def scheduled_at(self) -> str:
        ts = self.resume_at or (datetime.now(UTC) + timedelta(seconds=self.delay_seconds))
        return ts.isoformat()


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
    "native.schedule_update": (
        "Update the cron_expr and/or prompt of an existing schedule. "
        "Use native.schedule_list to find the schedule_id first."
    ),
    "native.schedule_delete": "Delete a schedule by ID. Use native.schedule_list to find the schedule_id.",
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
    "native.file_read": (
        "Read a file from session storage. Use offset/limit (head), tail (last N lines), "
        "or grep (lines containing substring) to extract exactly what you need. "
        "Text files return text; binary files return base64. "
        "Capped at 10MB / 50000 lines — use surgical params to avoid truncation."
    ),
    "native.file_write": (
        "Write content to a file in session storage. Overwrites if the file already exists. "
        "Content is text — binary data should be base64-encoded."
    ),
    "native.file_append": (
        "Append content to an existing file in session storage. "
        "Creates the file if it doesn't exist."
    ),
    "native.file_list": (
        "List files in session storage. Use prefix to filter by subdirectory "
        "(e.g. 'reports/' for files under the reports directory)."
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
        },
        "required": ["cron_expr", "prompt"],
    },
    "native.schedule_update": {
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "description": "UUID of the schedule to update."},
            "cron_expr": {
                "type": "string",
                "description": "New cron expression. Omit to keep existing.",
            },
            "prompt": {"type": "string", "description": "New prompt. Omit to keep existing."},
        },
        "required": ["schedule_id"],
    },
    "native.schedule_delete": {
        "type": "object",
        "properties": {
            "schedule_id": {"type": "string", "description": "UUID of the schedule to delete."},
        },
        "required": ["schedule_id"],
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
    "native.file_read": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path within session storage."},
            "offset": {
                "type": "integer",
                "description": "Start reading from this line number (0-indexed).",
            },
            "limit": {
                "type": "integer",
                "description": "Max lines to return.",
            },
            "tail": {
                "type": "integer",
                "description": "Return last N lines of the file.",
            },
            "grep": {
                "type": "string",
                "description": "Return only lines containing this substring.",
            },
        },
        "required": ["path"],
    },
    "native.file_write": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path within session storage."},
            "content": {"type": "string", "description": "Text content to write."},
            "content_type": {
                "type": "string",
                "description": "MIME type, e.g. text/html, application/json. Default: text/plain.",
            },
        },
        "required": ["path", "content"],
    },
    "native.file_append": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path within session storage."},
            "content": {"type": "string", "description": "Text content to append."},
            "content_type": {
                "type": "string",
                "description": "MIME type, e.g. text/html, application/json. Default: text/plain.",
            },
        },
        "required": ["path", "content"],
    },
    "native.file_list": {
        "type": "object",
        "properties": {
            "prefix": {
                "type": "string",
                "description": "Filter files by path prefix (e.g. 'reports/').",
            },
            "max_results": {
                "type": "integer",
                "description": "Max files to return (default 200).",
            },
        },
    },
}
