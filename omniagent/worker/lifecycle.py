"""Session lifecycle — complete turn, handle defer, cancel logic.

These functions own session status transitions end-to-end. Only the worker
writes terminal statuses; the API only reads them. All transitions use
``SELECT ... FOR UPDATE`` to serialize concurrent operations.
"""

import json
import logging
from datetime import UTC, datetime

from omniagent.api.models import MessageRecord
from omniagent.db import get_conn
from omniagent.worker.events import _CH, _emit_event
from omniagent.worker.models import BaseEvent
from omniagent.worker.native import DeferInfo

logger = logging.getLogger(__name__)

_CANCEL_MARKER = "[CANCELLED: previous response was stopped by the user before completing]"


async def _complete_session(session_id: str, result: str, prior_count: int) -> None:
    """Append the assistant reply and decide whether to go idle or chain another turn.

    ``prior_count`` is the message count this turn started with. If the array
    has grown beyond that (via /run appending while this turn was in flight),
    there's unanswered input queued — go back to 'pending' and schedule an
    immediate follow-up turn instead of 'idle', so nothing sent while busy
    gets silently dropped. ``status`` is job-owned end to end: only this
    function (or ``_handle_defer``) ever writes it, always a confirmed fact, so
    a fresh SSE listener can always trust it immediately, cancelled included.

    If cancellation was requested while this turn was in flight, this turn's
    own answer is stale and gets discarded — that only stops THIS turn, same
    as "stop generating" in a chat UI. Anything queued in the meantime still
    gets picked up by an immediate follow-up turn, exactly like the non
    cancelled catch-up path below.
    """
    ch = _CH(session_id)
    has_queued_input = False
    now = datetime.now(UTC).isoformat()
    async with get_conn() as conn:
        # ponytail: jsonb_array_length avoids transferring full messages array.
        # FOR UPDATE still required for cancel_requested atomicity.
        rows = await conn.execute(
            "SELECT jsonb_array_length(messages) as msg_count, cancel_requested "
            "FROM sessions WHERE id=%s FOR UPDATE",
            (session_id,),
        )
        sess = await rows.fetchone()
        if not sess:
            return
        has_queued_input = (sess["msg_count"] or 0) > prior_count

        if sess["cancel_requested"]:
            logger.info("session %s cancel requested, discarding this turn's result", session_id)
            marker_json = json.dumps(
                MessageRecord(role="user", content=_CANCEL_MARKER, timestamp=now).model_dump()
            )
            next_status = "pending" if has_queued_input else "cancelled"
            clear_trace = "" if has_queued_input else ", langfuse_trace_id = NULL"
            await conn.execute(
                f"UPDATE sessions SET status=%s, "
                f"messages = jsonb_insert(messages, %s::text[], %s::jsonb), "
                f"cancel_requested=false, updated_at=NOW(){clear_trace} WHERE id=%s",
                (next_status, f"{{{prior_count}}}", f"[{marker_json}]", session_id),
            )
            # Always 'cancelled' here — the first turn is stopped, full stop.
            # If there's queued input, the follow-up turn's own lifecycle
            # (run_agent_job → 'running', then 'complete') handles the rest.
            # This gives the client a clean gap between "stopping" (first
            # message done) and "stop" (second message starting), so the user
            # can stop the second message independently.
            await conn.execute("SELECT pg_notify(%s, %s)", (ch, "cancelled"))
        else:
            assistant_json = json.dumps(
                MessageRecord(role="assistant", content=result, timestamp=now).model_dump()
            )
            next_status = "pending" if has_queued_input else "idle"
            clear_trace = "" if has_queued_input else ", langfuse_trace_id = NULL"
            await conn.execute(
                f"UPDATE sessions SET status=%s, "
                f"messages = messages || %s::jsonb, "
                f"updated_at=NOW(){clear_trace} WHERE id=%s",
                (next_status, f"[{assistant_json}]", session_id),
            )
            await conn.execute("SELECT pg_notify(%s, %s)", (ch, "complete"))

    if has_queued_input:
        from omniagent.worker.job import run_agent_job  # lazy, avoids circular import

        await run_agent_job.configure(queue="default").defer_async(session_id=session_id)
        logger.info("session %s has queued input, scheduling follow-up turn", session_id)


async def _handle_defer(
    session_id: str,
    result: str,
    history: list[MessageRecord],
    defer: DeferInfo,
) -> None:
    """Persist the deferred turn's outcome and re-arm the session for wake-up.

    Re-fetches messages fresh (under FOR UPDATE) instead of overwriting from
    the stale ``history`` snapshot — anything appended via /run while this turn
    was in flight must survive, not get silently erased by this write.

    If cancellation was requested mid-turn, its decision to defer is
    discarded — same as ``_complete_session``, cancel only stops this turn, not
    the conversation. Any messages queued in the meantime get an immediate
    follow-up turn instead of waiting for the (now-discarded) defer's
    wake-up time.
    """
    now = datetime.now(UTC).isoformat()
    prior_count = len(history)
    cancelled = False
    cancelled_with_queued_input = False

    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT jsonb_array_length(messages) as msg_count, cancel_requested "
            "FROM sessions WHERE id=%s FOR UPDATE",
            (session_id,),
        )
        sess = await rows.fetchone()
        if not sess:
            return
        cancelled = sess["cancel_requested"]

        if cancelled:
            logger.info("session %s cancel requested, discarding defer", session_id)
            marker_json = json.dumps(
                MessageRecord(role="user", content=_CANCEL_MARKER, timestamp=now).model_dump()
            )
            cancelled_with_queued_input = (sess["msg_count"] or 0) > prior_count
            next_status = "pending" if cancelled_with_queued_input else "cancelled"
            clear_trace = "" if cancelled_with_queued_input else ", langfuse_trace_id = NULL"
            await conn.execute(
                f"UPDATE sessions SET status=%s, "
                f"messages = jsonb_insert(messages, %s::text[], %s::jsonb), "
                f"cancel_requested=false, updated_at=NOW(){clear_trace} WHERE id=%s",
                (next_status, f"{{{prior_count}}}", f"[{marker_json}]", session_id),
            )
        else:
            # ponytail: defer non-cancel path does complex splicing (truncate +
            # append assistant + extend queued + append resume marker). Rare —
            # only when agent calls defer_turn. Full array transfer is fine.
            rows = await conn.execute("SELECT messages FROM sessions WHERE id=%s", (session_id,))
            sess2 = await rows.fetchone()
            if not sess2:
                return
            current_messages = sess2["messages"] or []
            queued = current_messages[prior_count:]
            new_messages = current_messages[:prior_count]
            new_messages.append(
                MessageRecord(role="assistant", content=result, timestamp=now).model_dump()
            )
            new_messages.extend(queued)
            new_messages.append(
                MessageRecord(
                    role="user",
                    content="[RESUME: Turn resumed. Continue your task.]",
                    timestamp=now,
                ).model_dump()
            )
            await conn.execute(
                "UPDATE sessions SET status='deferred', messages=%s, deferred_payload=%s, updated_at=NOW() WHERE id=%s",
                (json.dumps(new_messages), "{}", session_id),
            )

    if cancelled:
        ch = _CH(session_id)
        async with get_conn() as conn:
            # Same as _complete_session — always 'cancelled'. If there's
            # queued input the follow-up's own lifecycle fires 'running'.
            await conn.execute("SELECT pg_notify(%s, %s)", (ch, "cancelled"))
        if cancelled_with_queued_input:
            from omniagent.worker.job import run_agent_job  # lazy, avoids circular import

            await run_agent_job.configure(queue="default").defer_async(session_id=session_id)
        return

    await _emit_event(session_id, BaseEvent(type="deferred"))

    scheduled_at_iso = defer.scheduled_at()
    scheduled_at_dt = datetime.fromisoformat(scheduled_at_iso)
    from omniagent.worker.job import run_agent_job  # lazy, avoids circular import

    await run_agent_job.configure(queue="default", schedule_at=scheduled_at_dt).defer_async(
        session_id=session_id,
    )
    logger.info("session %s deferred until %s", session_id, scheduled_at_iso)
