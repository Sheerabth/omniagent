"""Procrastinate periodic task: fire due schedules every minute."""

import json
import logging
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel

from omniagent.worker.job import app

logger = logging.getLogger(__name__)


class _ScheduleRow(BaseModel):
    id: uuid.UUID
    agent_name: str
    cron_expr: str
    prompt: str


@app.periodic(cron="* * * * *")
@app.task(name="check_schedules", queue="default")
async def check_schedules(timestamp: int) -> None:
    from croniter import croniter

    from omniagent.api.db import get_conn

    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT id, agent_name, cron_expr, prompt FROM schedules WHERE enabled = TRUE AND (next_run_at IS NULL OR next_run_at <= NOW())"
        )
        schedules = [_ScheduleRow.model_validate(dict(r)) for r in await rows.fetchall()]

    for sched in schedules:
        try:
            await _fire_schedule(sched)

            # Compute next_run_at from cron expression
            c = croniter(sched.cron_expr)
            next_run = datetime.fromtimestamp(c.get_next(float), tz=UTC)

            async with get_conn() as conn:
                await conn.execute(
                    "UPDATE schedules SET last_run_at=NOW(), next_run_at=%s, updated_at=NOW() WHERE id=%s",
                    (next_run, sched.id),
                )
            logger.info("schedule %s fired, next=%s", sched.id, next_run.isoformat())
        except Exception:
            logger.exception("schedule %s: fire failed", sched.id)


async def _fire_schedule(sched: _ScheduleRow) -> None:
    from omniagent.api.db import get_conn

    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT name, version, toolbox_refs, tool_refs FROM agents WHERE name = %s ORDER BY created_at DESC LIMIT 1",
            (sched.agent_name,),
        )
        agent = await rows.fetchone()
        if not agent:
            raise RuntimeError(f"agent not found: {sched.agent_name}")

        active = await (
            await conn.execute(
                "SELECT id FROM sessions WHERE schedule_id=%s AND status IN ('pending','running') LIMIT 1",
                (sched.id,),
            )
        ).fetchone()
        if active:
            logger.info("schedule %s: previous run still active, skipping", sched.id)
            return

        now = datetime.now(UTC).isoformat()
        messages = [{"role": "user", "content": sched.prompt, "timestamp": now}]

        rows = await conn.execute(
            """INSERT INTO sessions (agent_name, agent_version, toolbox_versions, tool_refs, status, messages, schedule_id, is_scheduled)
               VALUES (%s, %s, %s, %s, 'pending', %s, %s, TRUE) RETURNING id""",
            (
                agent["name"],
                agent["version"],
                json.dumps(agent["toolbox_refs"] or {}),
                agent["tool_refs"] or [],
                json.dumps(messages),
                sched.id,
            ),
        )
        session_id = str((await rows.fetchone())["id"])

    from omniagent.worker.job import run_agent_job

    await run_agent_job.configure(queue="default").defer_async(
        session_id=session_id,
    )
    logger.info("schedule %s: created session %s", sched.id, session_id)
