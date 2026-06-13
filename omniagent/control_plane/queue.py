"""Procrastinate setup for control plane (monitor-only — no job execution)."""
import logging

logger = logging.getLogger(__name__)

_session_fail_callback = None


def set_session_fail_callback(cb) -> None:
    global _session_fail_callback
    _session_fail_callback = cb


async def on_job_failure_handler(job) -> None:
    session_id = (job.task_kwargs or {}).get("session_id")
    if not session_id or _session_fail_callback is None:
        return
    logger.warning("queue: job failed for session %s", session_id)
    await _session_fail_callback(session_id)
