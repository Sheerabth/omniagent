"""Procrastinate periodic task: fire due schedules every minute."""

import json
import logging
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from omniagent.config import settings
from omniagent.worker.job import app
from omniagent.worker.queries import (
    insert_session_from_schedule,
    select_active_session_by_schedule,
    select_agent_by_name_latest,
    select_due_schedules,
    update_schedule_after_fire,
)

logger = logging.getLogger(__name__)


class _ScheduleRow(BaseModel):
    id: uuid.UUID
    agent_name: str
    cron_expr: str
    prompt: str


TASK_NAME = "check_schedules"


@app.periodic(cron="* * * * *")
@app.task(name=TASK_NAME, queue=settings.worker_queue_name)
async def check_schedules(timestamp: int) -> None:
    from croniter import croniter

    from omniagent.db import get_conn

    async with get_conn() as conn:
        result = await conn.execute(
            select_due_schedules,
        )
        schedules_rows = [
            _ScheduleRow.model_validate(dict(r)) for r in result.mappings().fetchall()
        ]

    for sched in schedules_rows:
        try:
            await _fire_schedule(sched)

            # Compute next_run_at from cron expression
            c = croniter(sched.cron_expr)
            next_run = datetime.fromtimestamp(c.get_next(float), tz=UTC)

            async with get_conn() as conn:
                await conn.execute(
                    update_schedule_after_fire,
                    {"schedule_id": sched.id, "next_run_at": next_run},
                )
            logger.info("schedule %s fired, next=%s", sched.id, next_run.isoformat())
        except Exception:
            logger.exception("schedule %s: fire failed", sched.id)


async def _fire_schedule(sched: _ScheduleRow) -> None:
    from omniagent.db import get_conn

    async with get_conn() as conn:
        result = await conn.execute(
            select_agent_by_name_latest,
            {"name": sched.agent_name},
        )
        agent = result.mappings().fetchone()
        if not agent:
            raise RuntimeError(f"agent not found: {sched.agent_name}")

        result = await conn.execute(
            select_active_session_by_schedule,
            {"schedule_id": sched.id},
        )
        active = result.fetchone()
        if active:
            logger.info("schedule %s: previous run still active, skipping", sched.id)
            return

        now = datetime.now(UTC).isoformat()
        messages = [{"role": "user", "content": sched.prompt, "timestamp": now}]

        result = await conn.execute(
            insert_session_from_schedule,
            {
                "agent_name": agent["name"],
                "agent_version": agent["version"],
                "toolbox_versions": json.dumps(agent["toolbox_refs"] or {}),
                "tool_refs": agent["tool_refs"] or [],
                "messages": json.dumps(messages),
                "schedule_id": sched.id,
            },
        )
        session_row = result.mappings().fetchone()
        assert session_row is not None, "INSERT RETURNING returned no row"
        session_id = str(session_row["id"])

    from omniagent.worker.job import run_agent_job

    await run_agent_job.configure(queue=settings.worker_queue_name).defer_async(
        session_id=session_id,
    )
    logger.info("schedule %s: created session %s", sched.id, session_id)
