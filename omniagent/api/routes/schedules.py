"""CRUD for scheduled agent runs."""

import uuid

from fastapi import APIRouter, Depends, HTTPException

from omniagent.api.auth import require_scope
from omniagent.api.models import ScheduleCreate, ScheduleRecord, ScheduleUpdate
from omniagent.api.queries import (
    build_cancel_running_sessions_by_schedule,
    delete_schedule_by_id,
    insert_schedule,
    select_orphaned_scheduled_sessions,
    select_schedule_by_id,
    select_schedules,
    select_sessions_by_schedule,
    update_schedule_returning,
)
from omniagent.constants import SessionStatus
from omniagent.crypto import encrypt_auth_context
from omniagent.db import get_conn

router = APIRouter(prefix="/schedules", tags=["schedules"])


def _next_run_at(cron_expr: str) -> str | None:
    """Compute next cron fire time using croniter (transitive dep via procrastinate)."""
    try:
        from datetime import UTC, datetime

        from croniter import croniter

        c = croniter(cron_expr)
        return datetime.fromtimestamp(c.get_next(float), tz=UTC).isoformat()
    except Exception:
        return None


@router.post("", response_model=ScheduleRecord, status_code=201)
async def create_schedule(
    body: ScheduleCreate, _=Depends(require_scope("agents:write"))
) -> ScheduleRecord:
    next_run = _next_run_at(body.cron_expr)
    encrypted_auth = encrypt_auth_context(body.auth_context)

    async with get_conn() as conn:
        rows = await conn.execute(
            insert_schedule,
            (body.agent_name, body.cron_expr, body.prompt, encrypted_auth, body.enabled, next_run),
        )
        row = await rows.fetchone()
        assert row is not None
        return ScheduleRecord.model_validate(row)


@router.get("", response_model=list[ScheduleRecord])
async def list_schedules(_=Depends(require_scope("agents:read"))) -> list[ScheduleRecord]:
    async with get_conn() as conn:
        rows = await conn.execute(select_schedules)
        return [ScheduleRecord.model_validate(dict(r)) for r in await rows.fetchall()]


@router.get("/orphaned-runs", response_model=list)
async def list_orphaned_runs(_=Depends(require_scope("agents:read"))) -> list:
    from omniagent.api.models import SessionRecord

    async with get_conn() as conn:
        rows = await conn.execute(select_orphaned_scheduled_sessions)
        return [
            SessionRecord.model_validate(dict(r)).model_dump(mode="json")
            for r in await rows.fetchall()
        ]


@router.get("/{schedule_id}", response_model=ScheduleRecord)
async def get_schedule(
    schedule_id: uuid.UUID, _=Depends(require_scope("agents:read"))
) -> ScheduleRecord:
    async with get_conn() as conn:
        rows = await conn.execute(select_schedule_by_id, (schedule_id,))
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return ScheduleRecord.model_validate(dict(row))


@router.patch("/{schedule_id}", response_model=ScheduleRecord)
async def update_schedule(
    schedule_id: uuid.UUID, body: ScheduleUpdate, _=Depends(require_scope("agents:write"))
) -> ScheduleRecord:
    sets = []
    vals = []
    if body.cron_expr is not None:
        sets.append("cron_expr=%s")
        vals.append(body.cron_expr)
        next_run = _next_run_at(body.cron_expr)
        sets.append("next_run_at=%s")
        vals.append(next_run)
    if body.prompt is not None:
        sets.append("prompt=%s")
        vals.append(body.prompt)
    if body.enabled is not None:
        sets.append("enabled=%s")
        vals.append(body.enabled)
    if not sets:
        raise HTTPException(422, detail="No fields to update")

    sets.append("updated_at=NOW()")
    vals.append(schedule_id)

    async with get_conn() as conn:
        rows = await conn.execute(
            update_schedule_returning.format(sets=", ".join(sets)),
            vals,
        )
        row = await rows.fetchone()
    if not row:
        raise HTTPException(404)
    return ScheduleRecord.model_validate(dict(row))


@router.get("/{schedule_id}/runs", response_model=list)
async def list_schedule_runs(
    schedule_id: uuid.UUID, _=Depends(require_scope("agents:read"))
) -> list:
    from omniagent.api.models import SessionRecord

    async with get_conn() as conn:
        rows = await conn.execute(select_sessions_by_schedule, (schedule_id,))
        return [
            SessionRecord.model_validate(dict(r)).model_dump(mode="json")
            for r in await rows.fetchall()
        ]


@router.delete("/{schedule_id}", status_code=204)
async def delete_schedule(schedule_id: uuid.UUID, _=Depends(require_scope("agents:write"))) -> None:
    async with get_conn() as conn, conn.transaction():
        await conn.execute(
            build_cancel_running_sessions_by_schedule(
                SessionStatus.CANCELLED.value
            ),  # pyright: ignore[reportArgumentType]
            (schedule_id,),
        )
        await conn.execute(delete_schedule_by_id, (schedule_id,))
