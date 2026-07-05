"""SSE event emission via pg_notify."""

import json
import logging
from datetime import UTC, datetime

from omniagent.api.models import ToolCallEntry
from omniagent.db import get_conn
from omniagent.worker.models import BaseEvent

logger = logging.getLogger(__name__)

_CH = lambda sid: "session_" + sid.replace("-", "_")  # noqa: E731


async def _emit_event(session_id: str, event: BaseEvent) -> None:
    ch = _CH(session_id)
    try:
        if event.type == "error":
            async with get_conn() as conn:
                await conn.execute(
                    "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s AND status='running'",
                    (session_id,),
                )
                await conn.execute("SELECT pg_notify(%s, %s)", (ch, "error"))
        elif event.type == "tool_result":
            ev = event.model_dump(exclude_none=True)
            if ev.get("input") is not None and "output" in ev:
                entry_json = json.dumps(
                    ToolCallEntry(
                        tool_name=ev.get("tool") or "",
                        input=ev.get("input") or {},
                        output=ev.get("output"),
                        harness=ev.get("harness"),
                        skill_name=ev.get("skill_name"),
                        timestamp=datetime.now(UTC),
                        success=ev.get("success", True),
                        error=ev.get("error"),
                    ).model_dump(mode="json")
                )
                async with get_conn() as conn:
                    await conn.execute(
                        "UPDATE sessions SET tool_calls = tool_calls || %s::jsonb WHERE id=%s",
                        (f"[{entry_json}]", session_id),
                    )
                    await conn.execute("SELECT pg_notify(%s, %s)", (ch, "update"))
        else:
            async with get_conn() as conn:
                await conn.execute("SELECT pg_notify(%s, %s)", (ch, event.type))
    except Exception as exc:
        logger.warning("emit_event failed: %s", exc)
