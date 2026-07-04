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
from omniagent.api.db import get_conn

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sse"])

_CH = lambda sid: "session_" + str(sid).replace("-", "_")  # noqa: E731

# status is job-owned end to end (see job.py:_complete_session /
# _handle_defer) — the worker is the only writer, always a confirmed fact
# about its own lifecycle, cancelled included. So a resting status can
# always be trusted immediately, uniformly, no special-casing per value: no
# writer sets a status here that it might later revise.
RESTING = ("idle", "failed", "cancelled")


@router.get("/sessions/{session_id}/stream")
async def stream_session(
    session_id: uuid.UUID, _=Depends(require_scope("sessions:read"))
) -> EventSourceResponse:
    async with get_conn() as conn:
        rows = await conn.execute("SELECT status FROM sessions WHERE id = %s", (session_id,))
        sess = await rows.fetchone()
    if not sess:
        raise HTTPException(404)

    async def event_generator() -> AsyncGenerator[dict[str, str]]:
        channel = _CH(session_id)
        # Subscribe before re-checking status to avoid a race with a notify
        # firing in between.
        queue = await sse_hub.subscribe(channel)
        try:
            async with get_conn() as conn:
                rows = await conn.execute(
                    "SELECT status FROM sessions WHERE id = %s", (session_id,)
                )
                current = await rows.fetchone()

            if current and current["status"] in RESTING:
                ntype = "complete" if current["status"] == "idle" else current["status"]
                yield {"data": json.dumps({"type": ntype})}
                return

            # 300s is a LISTEN checkpoint, not a session deadline — long tool
            # calls and long defer_turn_until windows legitimately go quiet
            # for longer than that. Re-check real status after every
            # notification (not just on timeout) and only stop once it's
            # confirmed resting — a notification's type alone (e.g.
            # "complete") doesn't guarantee the session won't immediately
            # chain into another turn (see _complete_session's catch-up path).
            #
            # IMPORTANT: when the status recheck finds a resting state, we
            # must emit the terminal event before closing if the last emitted
            # event wasn't already terminal. Example failure case without
            # this: a `update` or `cancelling` is dequeued and yielded, then
            # the status recheck finds `idle`/`cancelled` — the stream would
            # close without the client ever getting `complete`/`cancelled`,
            # leaving the UI stuck (thinking dots, "Stopping…" button, stale
            # session badge). Only skip re-emitting if the event we just sent
            # was already a terminal one to avoid sending it twice.
            TERMINAL_EVENTS = {"complete", "error", "cancelled"}
            last_emitted: str | None = None
            while True:
                try:
                    ntype = await asyncio.wait_for(queue.get(), timeout=300)
                    last_emitted = ntype
                    yield {"data": json.dumps({"type": ntype})}
                except TimeoutError:
                    pass

                async with get_conn() as conn:
                    rows = await conn.execute(
                        "SELECT status FROM sessions WHERE id = %s", (session_id,)
                    )
                    current = await rows.fetchone()
                if not current:
                    return
                if current["status"] in RESTING:
                    if last_emitted not in TERMINAL_EVENTS:
                        terminal = "complete" if current["status"] == "idle" else current["status"]
                        yield {"data": json.dumps({"type": terminal})}
                    return
                # still running/pending/deferred — keep listening
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("SSE error for session %s: %s", session_id, e)
            yield {"data": json.dumps({"type": "error", "reason": str(e)})}
        finally:
            await sse_hub.unsubscribe(channel, queue)

    return EventSourceResponse(event_generator())
