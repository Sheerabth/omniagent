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
    select_agent_latest_by_name,
    select_schedule_active_session,
    select_schedules_due,
    update_schedule_fired,
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
        rows = await conn.execute(select_schedules_due)
        schedules = [_ScheduleRow.model_validate(dict(r)) for r in await rows.fetchall()]

    for sched in schedules:
        try:
            await _fire_schedule(sched)

            # Compute next_run_at from cron expression
            c = croniter(sched.cron_expr)
            next_run = datetime.fromtimestamp(c.get_next(float), tz=UTC)

            async with get_conn() as conn:
                await conn.execute(
                    update_schedule_fired,  # pyright: ignore[reportArgumentType]
                    (next_run, sched.id),
                )
            logger.info("schedule %s fired, next=%s", sched.id, next_run.isoformat())
        except Exception:
            logger.exception("schedule %s: fire failed", sched.id)


async def _fire_schedule(sched: _ScheduleRow) -> None:
    from omniagent.db import get_conn

    async with get_conn() as conn:
        rows = await conn.execute(
            select_agent_latest_by_name,  # pyright: ignore[reportArgumentType]
            (sched.agent_name,),
        )
        agent = await rows.fetchone()
        if not agent:
            raise RuntimeError(f"agent not found: {sched.agent_name}")

        active = await (
            await conn.execute(
                select_schedule_active_session,  # pyright: ignore[reportArgumentType]
                (sched.id,),
            )
        ).fetchone()
        if active:
            logger.info("schedule %s: previous run still active, skipping", sched.id)
            return

        now = datetime.now(UTC).isoformat()
        messages = [{"role": "user", "content": sched.prompt, "timestamp": now}]

        rows = await conn.execute(
            insert_session_from_schedule,  # pyright: ignore[reportArgumentType]
            (
                agent["name"],
                agent["version"],
                json.dumps(agent["toolbox_refs"] or {}),
                agent["tool_refs"] or [],
                json.dumps(messages),
                sched.id,
            ),
        )
        session_row = await rows.fetchone()
        assert session_row is not None, "INSERT RETURNING returned no row"
        session_id = str(session_row["id"])

    from omniagent.worker.job import run_agent_job

    await run_agent_job.configure(queue=settings.worker_queue_name).defer_async(
        session_id=session_id,
    )
    logger.info("schedule %s: created session %s", sched.id, session_id)
