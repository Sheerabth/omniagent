"""Procrastinate setup for control plane (monitor-only — no job execution)."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

SessionFailCallback = Callable[[str], Awaitable[None]]
_session_fail_callback: SessionFailCallback | None = None


def set_session_fail_callback(cb: SessionFailCallback) -> None:
    global _session_fail_callback
    _session_fail_callback = cb


async def on_job_failure_handler(job: Any) -> None:
    session_id = (job.task_kwargs or {}).get("session_id")
    if not session_id or _session_fail_callback is None:
        return
    logger.warning("queue: job failed for session %s", session_id)
    await _session_fail_callback(session_id)
