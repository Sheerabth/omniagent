"""Tool execution — HTTP-based external tools and native tool handlers."""

import base64
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from omniagent.config import settings
from omniagent.db import get_conn
from omniagent.worker.auth import _get_oauth_token, _get_oidc_token
from omniagent.worker.http import _get_http_client
from omniagent.worker.models import EventEmitter, ToolCallEvent, ToolResultEvent, ToolSnapshot
from omniagent.worker.native import NATIVE_TOOL_DESCRIPTIONS, DeferInfo

logger = logging.getLogger(__name__)

_DEFAULT_TOOL_TIMEOUT = settings.tool_execution_timeout


async def _tool_executor(
    _session_id: str,
    tool_name: str,
    input_data: dict[str, Any],
    tool_snapshot: dict[str, ToolSnapshot],
    _agent_name: str = "",
) -> Any:
    tool = tool_snapshot.get(tool_name)
    if not tool:
        raise RuntimeError(f"tool_not_found:{tool_name}")
    auth_context = tool.auth_context

    path_params = set(re.findall(r"\{(\w+)\}", tool.openapi_path))
    if missing := path_params - input_data.keys():
        raise RuntimeError(f"missing_path_params:{missing}")
    url = tool.openapi_base_url.rstrip("/") + tool.openapi_path
    for k in path_params:
        url = url.replace(f"{{{k}}}", quote(str(input_data[k]), safe=""))

    remaining = {k: v for k, v in input_data.items() if k not in path_params}
    method = tool.openapi_method
    props = (tool.input_schema or {}).get("properties", {})
    query_params: dict[str, Any] = {}
    body_params: dict[str, Any] = {}
    header_params: dict[str, str] = {}
    cookie_params: dict[str, str] = {}
    for k, v in remaining.items():
        loc = props.get(k, {}).get("x-param-in")
        if loc == "body":
            body_params[k] = v
        elif loc == "query":
            query_params[k] = v
        elif loc == "header":
            header_params[k] = str(v)
        elif loc == "cookie":
            cookie_params[k] = str(v)
        else:
            # no annotation — fall back to method-based (tools imported before this change)
            if method in ("GET", "DELETE", "HEAD", "OPTIONS"):
                query_params[k] = v
            else:
                body_params[k] = v
    params = query_params or None
    json_body = body_params if body_params else None

    headers: dict[str, str] = {**header_params}
    sec = tool.openapi_security
    if sec and auth_context:
        try:
            if sec["type"] == "bearer":
                headers["Authorization"] = f"Bearer {auth_context[sec['token_key']]}"
            elif sec["type"] == "apiKey" and sec["in"] == "header":
                headers[sec["name"]] = auth_context[sec["token_key"]]
            elif sec["type"] == "apiKey" and sec["in"] == "query":
                params = {**(params or {}), sec["name"]: auth_context[sec["token_key"]]}
            elif sec["type"] == "apiKey" and sec["in"] == "cookie":
                headers["Cookie"] = f"{sec['name']}={auth_context[sec['token_key']]}"
            elif sec["type"] == "basic":
                creds = base64.b64encode(
                    f"{auth_context[sec['username_key']]}:{auth_context[sec['password_key']]}".encode()
                ).decode()
                headers["Authorization"] = f"Basic {creds}"
            elif sec["type"] == "oauth2":
                headers["Authorization"] = f"Bearer {await _get_oauth_token(sec, auth_context)}"
            elif sec["type"] == "oidc":
                headers["Authorization"] = f"Bearer {await _get_oidc_token(sec, auth_context)}"
            else:
                raise RuntimeError(f"unknown security type: {sec['type']!r}")
        except KeyError as e:
            raise RuntimeError(f"auth_context missing key {e} for tool {tool_name!r}") from e

    timeout = tool.timeout if tool.timeout is not None else _DEFAULT_TOOL_TIMEOUT
    resp = await _get_http_client().request(
        method,
        url,
        params=params,
        json=json_body,
        headers=headers,
        cookies=cookie_params or None,
        timeout=timeout,
    )
    if resp.status_code >= 500:
        resp.raise_for_status()
    if (
        not resp.content
        or resp.headers.get("content-type", "").split(";")[0].strip() != "application/json"
    ):
        return {"status": resp.status_code, "body": resp.text or None}
    return resp.json()


# ── Native tool execution ────────────────────────────────────────────────────


class NativeToolContext(BaseModel):
    """Immutable context for native tool execution within a single agent turn."""

    model_config = {"arbitrary_types_allowed": True}

    session_id: str
    agent_name: str
    harness: str
    tool_snapshot: dict[str, ToolSnapshot]
    defer_state: dict[str, DeferInfo]  # mutated in-place by defer_turn / defer_turn_until


class NativeToolExecutor:
    """Handles all native.* tool calls that bypass the HTTP executor.

    Each handler follows the same pattern:
    1. Emit ToolCallEvent
    2. Do DB work
    3. Emit ToolResultEvent
    4. Return result
    """

    def __init__(self, ctx: NativeToolContext, emit: EventEmitter) -> None:
        self._ctx = ctx
        self._emit = emit

    async def execute(self, tool_name: str, input_data: dict[str, Any]) -> Any:
        """Dispatch to the appropriate native handler or raise."""
        if tool_name not in NATIVE_TOOL_DESCRIPTIONS:
            raise RuntimeError(f"unknown_native_tool:{tool_name}")

        # Resolve external tool fallback (non-native tools go through HTTP executor)
        if tool_name not in _NATIVE_HANDLERS:
            return await self._external_tool(tool_name, input_data)

        handler_name = _NATIVE_HANDLERS[tool_name]
        handler = getattr(self, handler_name)
        return await handler(input_data)

    async def _external_tool(self, tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        """Fallback: execute a non-native tool via the HTTP executor."""
        skill_name = self._ctx.tool_snapshot[tool_name].skill_name
        await self._emit(
            ToolCallEvent(
                tool=tool_name,
                input=input_data,
                harness=self._ctx.harness,
                skill_name=skill_name or None,
            ),
        )
        output = await _tool_executor(
            self._ctx.session_id,
            tool_name,
            input_data,
            self._ctx.tool_snapshot,
            _agent_name=self._ctx.agent_name,
        )
        await self._emit(
            ToolResultEvent(
                tool=tool_name,
                success=True,
                input=input_data,
                output=output,
                harness=self._ctx.harness,
                skill_name=skill_name or None,
            ),
        )
        return output

    # ── Memory tools ──────────────────────────────────────────────────────

    async def _memory_get(self, input_data: dict[str, Any]) -> Any:
        await self._emit(
            ToolCallEvent(
                tool="native.memory_get",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        async with get_conn() as conn:
            rows = await conn.execute(
                "SELECT value FROM agent_memory WHERE agent_name=%s AND key=%s",
                (self._ctx.agent_name, input_data["key"]),
            )
            row = await rows.fetchone()
            result = row["value"] if row else None
        await self._emit(
            ToolResultEvent(
                tool="native.memory_get",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    async def _memory_set(self, input_data: dict[str, Any]) -> dict[str, Any]:
        await self._emit(
            ToolCallEvent(
                tool="native.memory_set",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        async with get_conn() as conn:
            await conn.execute(
                """INSERT INTO agent_memory (agent_name, key, value, updated_at)
                   VALUES (%s, %s, %s, NOW())
                   ON CONFLICT (agent_name, key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                (self._ctx.agent_name, input_data["key"], json.dumps(input_data["value"])),
            )
            result = {"ok": True}
        await self._emit(
            ToolResultEvent(
                tool="native.memory_set",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    async def _memory_delete(self, input_data: dict[str, Any]) -> dict[str, Any]:
        await self._emit(
            ToolCallEvent(
                tool="native.memory_delete",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        async with get_conn() as conn:
            await conn.execute(
                "DELETE FROM agent_memory WHERE agent_name=%s AND key=%s",
                (self._ctx.agent_name, input_data["key"]),
            )
            result = {"ok": True}
        await self._emit(
            ToolResultEvent(
                tool="native.memory_delete",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    async def _memory_list(self, input_data: dict[str, Any]) -> dict[str, Any]:
        await self._emit(
            ToolCallEvent(
                tool="native.memory_list",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        async with get_conn() as conn:
            rows = await conn.execute(
                "SELECT key FROM agent_memory WHERE agent_name=%s ORDER BY key",
                (self._ctx.agent_name,),
            )
            result = {"keys": [r["key"] for r in await rows.fetchall()]}
        await self._emit(
            ToolResultEvent(
                tool="native.memory_list",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    # ── Schedule tools ────────────────────────────────────────────────────

    async def _schedule_list(self, input_data: dict[str, Any]) -> list[dict[str, Any]]:
        await self._emit(
            ToolCallEvent(
                tool="native.schedule_list",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        async with get_conn() as conn:
            rows = await conn.execute(
                "SELECT * FROM schedules WHERE agent_name=%s ORDER BY created_at DESC",
                (self._ctx.agent_name,),
            )
            from omniagent.api.models import ScheduleRecord

            schedules = [ScheduleRecord.model_validate(dict(r)) for r in await rows.fetchall()]
            result: list[dict[str, Any]] = [
                {
                    "schedule_id": str(s.id),
                    "cron_expr": s.cron_expr,
                    "prompt": s.prompt,
                    "enabled": s.enabled,
                    "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                }
                for s in schedules
            ]
        await self._emit(
            ToolResultEvent(
                tool="native.schedule_list",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    async def _schedule_create(self, input_data: dict[str, Any]) -> dict[str, Any]:
        from croniter import croniter as _croniter

        cron_expr = input_data.get("cron_expr", "")
        prompt = input_data.get("prompt", "")
        try:
            c = _croniter(cron_expr)
            next_run = datetime.fromtimestamp(c.get_next(float), tz=UTC)
        except Exception as exc:
            raise RuntimeError(f"invalid cron_expr {cron_expr!r}: {exc}") from exc

        await self._emit(
            ToolCallEvent(
                tool="native.schedule_create",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        async with get_conn() as conn:
            rows = await conn.execute(
                """INSERT INTO schedules (agent_name, cron_expr, prompt, next_run_at)
                   VALUES (%s, %s, %s, %s) RETURNING id""",
                (self._ctx.agent_name, cron_expr, prompt, next_run),
            )
            schedule_row = await rows.fetchone()
            assert schedule_row is not None, "INSERT RETURNING returned no row"
            schedule_id = str(schedule_row["id"])
        result = {"schedule_id": schedule_id, "next_run_at": next_run.isoformat()}
        await self._emit(
            ToolResultEvent(
                tool="native.schedule_create",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    async def _schedule_update(self, input_data: dict[str, Any]) -> dict[str, Any]:
        from croniter import croniter as _croniter

        schedule_id = input_data.get("schedule_id")
        cron_expr = input_data.get("cron_expr")
        prompt = input_data.get("prompt")

        await self._emit(
            ToolCallEvent(
                tool="native.schedule_update",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        async with get_conn() as conn:
            row = await (
                await conn.execute(
                    "SELECT cron_expr, prompt FROM schedules WHERE id=%s AND agent_name=%s",
                    (schedule_id, self._ctx.agent_name),
                )
            ).fetchone()
            if not row:
                raise RuntimeError(f"schedule {schedule_id!r} not found")
            new_cron = cron_expr or row["cron_expr"]
            new_prompt = prompt if prompt is not None else row["prompt"]
            try:
                c = _croniter(new_cron)
                next_run = datetime.fromtimestamp(c.get_next(float), tz=UTC)
            except Exception as exc:
                raise RuntimeError(f"invalid cron_expr {new_cron!r}: {exc}") from exc
            await conn.execute(
                "UPDATE schedules SET cron_expr=%s, prompt=%s, next_run_at=%s WHERE id=%s AND agent_name=%s",
                (new_cron, new_prompt, next_run, schedule_id, self._ctx.agent_name),
            )
        result = {
            "schedule_id": schedule_id,
            "cron_expr": new_cron,
            "next_run_at": next_run.isoformat(),
        }
        await self._emit(
            ToolResultEvent(
                tool="native.schedule_update",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    async def _schedule_delete(self, input_data: dict[str, Any]) -> dict[str, Any]:
        schedule_id = input_data.get("schedule_id")
        await self._emit(
            ToolCallEvent(
                tool="native.schedule_delete",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        async with get_conn() as conn, conn.transaction():
            await conn.execute(
                "UPDATE sessions SET status='cancelled', updated_at=NOW() WHERE schedule_id=%s AND status='pending'",
                (schedule_id,),
            )
            res = await conn.execute(
                "DELETE FROM schedules WHERE id=%s AND agent_name=%s",
                (schedule_id, self._ctx.agent_name),
            )
            if res.rowcount == 0:
                raise RuntimeError(f"schedule {schedule_id!r} not found")
        result = {"schedule_id": schedule_id, "deleted": True}
        await self._emit(
            ToolResultEvent(
                tool="native.schedule_delete",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    # ── Defer tools ───────────────────────────────────────────────────────

    async def _defer_turn(self, input_data: dict[str, Any]) -> dict[str, Any]:
        delay = int(input_data.get("delay_seconds", 0))
        info = DeferInfo(delay_seconds=delay)
        self._ctx.defer_state["info"] = info
        await self._emit(
            ToolCallEvent(
                tool="native.defer_turn",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        result = {"status": "deferred", "resume_in_seconds": delay}
        await self._emit(
            ToolResultEvent(
                tool="native.defer_turn",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result

    async def _defer_turn_until(self, input_data: dict[str, Any]) -> dict[str, Any]:
        ts_str = input_data.get("iso_timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RuntimeError(f"invalid iso_timestamp: {ts_str!r}") from exc
        info = DeferInfo(resume_at=ts)
        self._ctx.defer_state["info"] = info
        await self._emit(
            ToolCallEvent(
                tool="native.defer_turn_until",
                input=input_data,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        result = {"status": "deferred", "resume_at": ts.isoformat()}
        await self._emit(
            ToolResultEvent(
                tool="native.defer_turn_until",
                success=True,
                input=input_data,
                output=result,
                harness=self._ctx.harness,
                skill_name="native",
            ),
        )
        return result


# Dispatch table — maps tool_name → handler method
_NATIVE_HANDLERS: dict[str, str] = {
    "native.memory_get": "_memory_get",
    "native.memory_set": "_memory_set",
    "native.memory_delete": "_memory_delete",
    "native.memory_list": "_memory_list",
    "native.schedule_list": "_schedule_list",
    "native.schedule_create": "_schedule_create",
    "native.schedule_update": "_schedule_update",
    "native.schedule_delete": "_schedule_delete",
    "native.defer_turn": "_defer_turn",
    "native.defer_turn_until": "_defer_turn_until",
}
