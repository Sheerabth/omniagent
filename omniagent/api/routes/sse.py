"""SSE streaming via PostgreSQL LISTEN/NOTIFY.

Uses the shared sse_hub connection instead of opening a dedicated raw
connection per stream — see sse_hub.py for why.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from sse_starlette.sse import EventSourceResponse

from omniagent.api import sse_hub
from omniagent.api.auth import require_scope
from omniagent.api.queries import select_session_for_update, select_session_status
from omniagent.constants import NotifyType, SessionStatus, session_channel
from omniagent.db import get_conn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])

# status is job-owned end to end (see job.py:_complete_session /
# _handle_defer) — the worker is the only writer, always a confirmed fact
# about its own lifecycle, cancelled included. So a resting status can
# always be trusted immediately, uniformly, no special-casing per value: no
# writer sets a status here that it might later revise.
RESTING = (SessionStatus.IDLE, SessionStatus.FAILED, SessionStatus.CANCELLED)


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: uuid.UUID, _=Depends(require_scope("sessions:read"))
) -> EventSourceResponse:
    async with get_conn() as conn:
        rows = await conn.execute(select_session_for_update, {"id": session_id})
        sess = rows.fetchone()
    if not sess:
        raise HTTPException(404)

    async def event_generator() -> AsyncGenerator[dict[str, str]]:
        channel = session_channel(session_id)
        # Subscribe before re-checking status to avoid a race with a notify
        # firing in between.
        queue = await sse_hub.subscribe(channel)
        try:
            async with get_conn() as conn:
                rows = await conn.execute(select_session_status, {"id": session_id})
                current = rows.mappings().fetchone()

            if current and current["status"] in RESTING:
                ntype = (
                    NotifyType.COMPLETE
                    if current["status"] == SessionStatus.IDLE
                    else current["status"]
                )
                yield {"data": json.dumps({"type": ntype, "final": True})}
                return

            # 300s is a LISTEN checkpoint, not a session deadline — long tool
            # calls and long defer_turn_until windows legitimately go quiet
            # for longer than that. A notification's type alone (e.g.
            # "complete") doesn't say whether the session is done for real or
            # about to chain into another turn (see _complete_session's
            # catch-up path), so `final` is always decided by re-checking
            # status, never by the notify string. Status is checked BEFORE
            # yielding: if it's resting, only the derived terminal event is
            # sent (final=True) — raw notifies that raced ahead of it (e.g. a
            # 'complete' immediately followed by 'running' from a chained
            # turn) are still forwarded first with final=False so the client
            # can render the intermediate turn instead of jumping straight to
            # the end state.
            while True:
                try:
                    pending = [await asyncio.wait_for(queue.get(), timeout=300)]
                except TimeoutError:
                    pending = []
                while True:
                    try:
                        pending.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                async with get_conn() as conn:
                    rows = await conn.execute(select_session_status, {"id": session_id})
                    current = rows.mappings().fetchone()
                if not current:
                    return

                for ntype in pending:
                    yield {"data": json.dumps({"type": ntype, "final": False})}

                if current["status"] in RESTING:
                    terminal = (
                        NotifyType.COMPLETE
                        if current["status"] == SessionStatus.IDLE
                        else current["status"]
                    )
                    yield {"data": json.dumps({"type": terminal, "final": True})}
                    return
                # still running/pending/deferred — keep listening
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("SSE error for session %s: %s", session_id, e)
            yield {"data": json.dumps({"type": NotifyType.ERROR, "reason": str(e), "final": True})}
        finally:
            await sse_hub.unsubscribe(channel, queue)

    return EventSourceResponse(event_generator())
