"""SSE event emission via pg_notify."""

import json
import logging
from datetime import UTC, datetime

from omniagent.api.models import ToolCallEntry
from omniagent.constants import EventType, NotifyType, session_channel
from omniagent.db import get_conn
from omniagent.worker.models import BaseEvent
from omniagent.worker.queries import (
    pg_notify,
    update_session_status_failed_running,
    update_session_tool_calls,
)

logger = logging.getLogger(__name__)


async def _emit_event(session_id: str, event: BaseEvent) -> None:
    ch = session_channel(session_id)
    try:
        if event.type == EventType.ERROR:
            async with get_conn() as conn:
                await conn.execute(
                    update_session_status_failed_running,  # pyright: ignore[reportArgumentType]
                    (session_id,),
                )
                await conn.execute(pg_notify, (ch, NotifyType.ERROR))
        elif event.type == EventType.TOOL_RESULT:
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
                        update_session_tool_calls,  # pyright: ignore[reportArgumentType]
                        (f"[{entry_json}]", session_id),
                    )
                    await conn.execute(pg_notify, (ch, NotifyType.UPDATE))
        else:
            async with get_conn() as conn:
                await conn.execute(pg_notify, (ch, event.type))
    except Exception as exc:
        logger.warning("emit_event failed: %s", exc)
