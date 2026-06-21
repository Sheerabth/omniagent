"""Procrastinate worker task: run_agent_job."""

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
import procrastinate
from procrastinate import PsycopgConnector

from omniagent.control_plane.crypto import decrypt_auth_context
from omniagent.control_plane.models import MessageRecord
from omniagent.worker.models import (
    BaseEvent,
    ErrorEvent,
    SessionConfig,
    SkillSnapshot,
    SystemPromptEvent,
    ToolCallEvent,
    ToolResultEvent,
    ToolSnapshot,
)
from omniagent.worker.native import NATIVE_TOOL_DESCRIPTIONS, NATIVE_TOOL_SCHEMAS, DeferInfo

logger = logging.getLogger(__name__)

CONTROL_PLANE = os.environ.get("OMNIAGENT_CONTROL_PLANE", "http://localhost:8080")
_DEFAULT_TOOL_TIMEOUT = int(os.environ.get("TOOL_EXECUTION_TIMEOUT", "30"))
INTERNAL_KEY = os.environ.get("OMNIAGENT_INTERNAL_KEY", "")

app = procrastinate.App(connector=PsycopgConnector(conninfo=os.environ.get("DATABASE_URL", "")))

_http_client: httpx.AsyncClient | None = None
_http_client_loop: asyncio.AbstractEventLoop | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client, _http_client_loop
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _http_client is None or _http_client_loop is not loop:
        _http_client = httpx.AsyncClient()
        _http_client_loop = loop
    return _http_client


def _internal_headers() -> dict[str, str]:
    """Headers for CP-internal calls — uses INTERNAL_KEY, never shared externally."""
    return {"X-OmniAgent-Key": INTERNAL_KEY}


async def _fetch_session_config(session_id: str) -> SessionConfig:
    from omniagent.control_plane.db import get_conn

    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT agent_name, agent_version, skill_versions FROM sessions WHERE id = %s",
            (session_id,),
        )
        session = await rows.fetchone()
        if not session:
            raise RuntimeError(f"session_not_found:{session_id}")

        agent_name = session["agent_name"]
        agent_version = session["agent_version"]
        skill_versions: dict[str, str] = session["skill_versions"] or {}

        rows = await conn.execute(
            "SELECT * FROM agents WHERE name = %s AND version = %s",
            (agent_name, agent_version),
        )
        agent = await rows.fetchone()
        if not agent:
            raise RuntimeError(f"agent_version_deleted:{agent_name}:{agent_version}")

        skills: list[SkillSnapshot] = []
        all_tool_names: list[str] = []
        tool_skill_name: dict[str, str] = {}
        for sname, skill_version in skill_versions.items():
            rows = await conn.execute(
                "SELECT * FROM skills WHERE name = %s AND version = %s",
                (sname, skill_version),
            )
            skill = await rows.fetchone()
            if not skill:
                raise RuntimeError(f"skill_version_deleted:{sname}:{skill_version}")
            skill_snap = SkillSnapshot.model_validate(dict(skill))
            skills.append(skill_snap)
            for t in skill["tool_names"]:
                all_tool_names.append(t)
                tool_skill_name[t] = sname

        tool_snapshot: dict[str, ToolSnapshot] = {}
        if all_tool_names:
            rows = await conn.execute("SELECT * FROM tools WHERE name = ANY(%s)", (all_tool_names,))
            for tool in await rows.fetchall():
                tool_snapshot[tool["name"]] = ToolSnapshot(
                    name=tool["name"],
                    description=tool["description"],
                    input_schema=tool["input_schema"],
                    output_schema=tool["output_schema"],
                    openapi_method=tool["openapi_method"],
                    openapi_path=tool["openapi_path"],
                    openapi_base_url=tool["openapi_base_url"],
                    openapi_security=tool["openapi_security"],
                    timeout=tool["timeout"],
                    skill_name=tool_skill_name.get(tool["name"], ""),
                )

    return SessionConfig(
        agent_name=agent_name,
        harness=agent["harness"],
        model=agent["model"],
        system_prompt=agent["system_prompt"],
        use_monty=agent["use_monty"],
        auth_context=decrypt_auth_context(agent.get("auth_context")),
        skills=skills,
        tool_snapshot=tool_snapshot,
    )


_oidc_discovery_cache: dict[str, str] = {}  # ponytail: process-local, discovery docs don't change


async def _get_oidc_token(security: dict, auth_context: dict) -> str:
    discovery_url = security["openid_connect_url"]
    if discovery_url not in _oidc_discovery_cache:
        resp = await _get_http_client().get(discovery_url, timeout=10)
        resp.raise_for_status()
        doc = resp.json()
        if "token_endpoint" not in doc:
            raise RuntimeError(f"OIDC discovery at {discovery_url} missing token_endpoint")
        _oidc_discovery_cache[discovery_url] = doc["token_endpoint"]
    token_url = _oidc_discovery_cache[discovery_url]
    return await _get_oauth_token({**security, "token_url": token_url}, auth_context)


async def _get_oauth_token(security: dict, auth_context: dict) -> str:
    # Use pre-stored token from auth code flow if present and not expired
    stored_token = auth_context.get("access_token")
    if stored_token:
        expiry = auth_context.get("token_expiry")
        if not expiry or time.time() < expiry - 30:
            return stored_token
    try:
        client_id = auth_context[security["client_id_key"]]
        client_secret = auth_context[security["client_secret_key"]]
    except KeyError as e:
        raise RuntimeError(f"auth_context missing key: {e}") from e
    cache_key = f"{security.get('token_url', '')}:{client_id}"

    from omniagent.control_plane.db import get_conn

    async with get_conn() as conn:
        row = await (
            await conn.execute(
                "SELECT token FROM oauth_token_cache WHERE cache_key=%s AND expires_at > NOW()",
                (cache_key,),
            )
        ).fetchone()
        if row:
            return row["token"]

    refresh_token = auth_context.get(security.get("refresh_token_key", "refresh_token"))
    payload: dict = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": " ".join(security.get("scopes", [])),
    }
    if refresh_token:
        payload["grant_type"] = "refresh_token"
        payload["refresh_token"] = refresh_token
    else:
        payload["grant_type"] = "client_credentials"
    resp = await _get_http_client().post(security["token_url"], data=payload)
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise RuntimeError(f"Token response missing access_token (got keys: {list(data.keys())})")
    token = data["access_token"]
    expires_in = data.get("expires_in", 3600)

    async with get_conn() as conn:
        await conn.execute("DELETE FROM oauth_token_cache WHERE expires_at < NOW()")
        await conn.execute(
            """INSERT INTO oauth_token_cache (cache_key, token, expires_at)
               VALUES (%s, %s, NOW() + %s * INTERVAL '1 second')
               ON CONFLICT (cache_key) DO UPDATE SET token=EXCLUDED.token, expires_at=EXCLUDED.expires_at""",
            (cache_key, token, expires_in - 30),
        )
    return token


async def _tool_executor(
    session_id: str,
    tool_name: str,
    input_data: dict[str, Any],
    tool_snapshot: dict[str, ToolSnapshot],
    auth_context: Any = None,
    agent_name: str = "",
) -> Any:
    tool = tool_snapshot.get(tool_name)
    if not tool:
        raise RuntimeError(f"tool_not_found:{tool_name}")

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


async def _emit_event(session_id: str, event: BaseEvent) -> None:
    try:
        await _get_http_client().post(
            f"{CONTROL_PLANE}/internal/sessions/{session_id}/event",
            json=event.model_dump(exclude_none=True),
            headers=_internal_headers(),
            timeout=5,
        )
    except Exception as exc:
        logger.warning("emit_event failed: %s", exc)


def _safe_tool_name(name: str) -> str:
    return name.replace(".", "__").replace("-", "_")


def _build_system_prompt(
    config: SessionConfig,
    llm_context: dict[str, Any] | None = None,
    extra_tools: dict[str, ToolSnapshot] | None = None,
) -> str:
    lines = [config.system_prompt]
    if llm_context:
        lines.append(f"\nUser context: {json.dumps(llm_context, default=str)}")

    for skill in config.skills:
        if skill.system_prompt:
            lines.append(skill.system_prompt)
        if skill.instructions:
            lines.append(skill.instructions)

    effective_snapshot = {**config.tool_snapshot, **(extra_tools or {})}

    if effective_snapshot and not config.use_monty:
        # Group tools by skill for clearer LLM context.
        by_skill: dict[str, list[tuple[str, ToolSnapshot]]] = {}
        for tool_name, schema in effective_snapshot.items():
            by_skill.setdefault(schema.skill_name or "", []).append((tool_name, schema))
        for skill_name, tools in by_skill.items():
            lines.append(f"\n## Skill: {skill_name}" if skill_name else "\nAvailable tools:")
            for tool_name, schema in tools:
                lines.append(f"- {tool_name}: {schema.description}")
                if schema.input_schema:
                    clean = {
                        **schema.input_schema,
                        "properties": {
                            k: {pk: pv for pk, pv in v.items() if pk != "x-param-in"}
                            for k, v in schema.input_schema.get("properties", {}).items()
                        },
                    }
                    lines.append(f"  Input schema: {json.dumps(clean)}")
                if schema.output_schema:
                    lines.append(f"  Output schema: {json.dumps(schema.output_schema)}")

    if effective_snapshot and config.use_monty:
        lines.append(
            "\nYou have access to one tool: `execute_python`. "
            "The sandbox exposes ONLY the functions listed below as globals — nothing else exists. "
            "There is NO internet, NO imports, NO urllib, NO requests, NO http, NO os, NO sys. "
            "Do NOT try to import anything. Do NOT probe the environment. Do NOT check available modules. "
            "JUST call the listed functions directly — they handle all networking and auth internally. "
            "ALWAYS write a SINGLE execute_python call that does everything — fetch all data, process it, and return the final answer. "
            "Never split work across multiple execute_python calls. "
            "Available functions (call these directly as globals):"
        )
        for tool_name, schema in effective_snapshot.items():
            fn = _safe_tool_name(tool_name)
            props = schema.input_schema.get("properties", {})
            params = ", ".join(f"{k}=..." for k in props)
            out_schema = schema.output_schema or {}
            out_props = out_schema.get("properties", out_schema)
            out_info = json.dumps(out_props) if out_props else "dict"
            lines.append(f"- {fn}({params})  -> {out_info}  # {schema.description}")
        lines.append(
            "\nIMPORTANT: Authentication (OAuth2, API keys, bearer tokens, basic auth) is handled AUTOMATICALLY "
            "by the framework. Do NOT attempt to fetch tokens or set headers manually. Just call the function directly.\n"
            "These functions return Python dicts DIRECTLY — there is NO 'result' wrapper key. "
            "Access fields directly, e.g.: `data = some_fn(x=1); value = data['field_name']`\n"
            "The LAST EXPRESSION in your code is the return value — it MUST be a variable or expression, NEVER a print() call. "
            "print() returns None and will discard all results. "
            "ALWAYS end your code with a bare variable name or expression: `result` not `print(result)`.\n"
            "ALWAYS include both `observation` (what you're doing) and `code` (the Python). "
            "Example: execute_python(observation='get Tokyo weather', code='result = get_weather(city=\"Tokyo\")\\nresult')"
        )

    return "\n".join(lines)


def _make_native_tool_snapshot(name: str) -> ToolSnapshot:
    return ToolSnapshot(
        name=name,
        description=NATIVE_TOOL_DESCRIPTIONS[name],
        input_schema=NATIVE_TOOL_SCHEMAS[name],
        output_schema={"type": "object"},
        skill_name="native",
        is_native=True,
    )


@app.task(name="run_agent_job", queue="default")
async def run_agent_job(session_id: str, payload: str) -> None:
    data = json.loads(payload)
    history = [MessageRecord.model_validate(m) for m in data.get("history", [])]
    runtime_auth_context: Any = decrypt_auth_context(data.get("auth_context"))
    runtime_llm_context: Any = data.get("llm_context")

    config = await _fetch_session_config(session_id)
    harness = config.harness
    model = config.model
    use_monty = config.use_monty

    # Runtime auth_context replaces agent default wholesale — no merge.
    auth_context = runtime_auth_context if runtime_auth_context is not None else config.auth_context

    # Inject native tools — must happen before building system prompt
    native_tools = {name: _make_native_tool_snapshot(name) for name in NATIVE_TOOL_DESCRIPTIONS}
    tool_snapshot = {**config.tool_snapshot, **native_tools}
    system_prompt = _build_system_prompt(config, runtime_llm_context, extra_tools=native_tools)

    llm_api_key = os.environ.get(f"OMNIAGENT_{harness.upper()}_API_KEY")

    # Shared defer state — set by native.defer_turn / native.defer_turn_until inside tool_exec
    _defer_state: dict[str, DeferInfo] = {}

    async def tool_exec(tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        # Native tools are handled here, not by the HTTP executor
        if tool_name in (
            "native.memory_get",
            "native.memory_set",
            "native.memory_delete",
            "native.memory_list",
        ):
            from omniagent.control_plane.db import get_conn

            await _emit_event(
                session_id,
                ToolCallEvent(
                    tool=tool_name, input=input_data, harness=harness, skill_name="native"
                ),
            )
            async with get_conn() as conn:
                if tool_name == "native.memory_get":
                    rows = await conn.execute(
                        "SELECT value FROM agent_memory WHERE agent_name=%s AND key=%s",
                        (config.agent_name, input_data["key"]),
                    )
                    row = await rows.fetchone()
                    result = row["value"] if row else None

                elif tool_name == "native.memory_set":
                    await conn.execute(
                        """INSERT INTO agent_memory (agent_name, key, value, updated_at)
                           VALUES (%s, %s, %s, NOW())
                           ON CONFLICT (agent_name, key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()""",
                        (config.agent_name, input_data["key"], json.dumps(input_data["value"])),
                    )
                    result = {"ok": True}

                elif tool_name == "native.memory_delete":
                    await conn.execute(
                        "DELETE FROM agent_memory WHERE agent_name=%s AND key=%s",
                        (config.agent_name, input_data["key"]),
                    )
                    result = {"ok": True}

                else:  # native.memory_list
                    rows = await conn.execute(
                        "SELECT key FROM agent_memory WHERE agent_name=%s ORDER BY key",
                        (config.agent_name,),
                    )
                    result = {"keys": [r["key"] for r in await rows.fetchall()]}

            await _emit_event(
                session_id,
                ToolResultEvent(
                    tool=tool_name,
                    success=True,
                    input=input_data,
                    output=result,
                    harness=harness,
                    skill_name="native",
                ),
            )
            return result

        if tool_name == "native.schedule_list":
            from omniagent.control_plane.db import get_conn

            await _emit_event(
                session_id,
                ToolCallEvent(
                    tool=tool_name, input=input_data, harness=harness, skill_name="native"
                ),
            )
            async with get_conn() as conn:
                rows = await conn.execute(
                    "SELECT id, cron_expr, prompt, enabled, next_run_at FROM schedules WHERE agent_name=%s ORDER BY created_at DESC",
                    (config.agent_name,),
                )
                result = [
                    {
                        "schedule_id": str(r["id"]),
                        "cron_expr": r["cron_expr"],
                        "prompt": r["prompt"],
                        "enabled": r["enabled"],
                        "next_run_at": r["next_run_at"].isoformat() if r["next_run_at"] else None,
                    }
                    for r in await rows.fetchall()
                ]
            await _emit_event(
                session_id,
                ToolResultEvent(
                    tool=tool_name,
                    success=True,
                    input=input_data,
                    output=result,
                    harness=harness,
                    skill_name="native",
                ),
            )
            return result

        if tool_name == "native.schedule_create":
            from croniter import croniter as _croniter

            from omniagent.control_plane.db import get_conn

            cron_expr = input_data.get("cron_expr", "")
            prompt = input_data.get("prompt", "")
            target_agent = config.agent_name
            llm_ctx_raw = input_data.get("llm_context")
            if llm_ctx_raw is not None:
                from psycopg.types.json import Jsonb

                llm_ctx = Jsonb(
                    llm_ctx_raw if isinstance(llm_ctx_raw, dict) else {"value": llm_ctx_raw}
                )
            else:
                llm_ctx = None
            try:
                c = _croniter(cron_expr)
                next_run = datetime.fromtimestamp(c.get_next(float), tz=UTC)
            except Exception as exc:
                raise RuntimeError(f"invalid cron_expr {cron_expr!r}: {exc}") from exc
            await _emit_event(
                session_id,
                ToolCallEvent(
                    tool=tool_name, input=input_data, harness=harness, skill_name="native"
                ),
            )
            async with get_conn() as conn:
                rows = await conn.execute(
                    """INSERT INTO schedules (agent_name, cron_expr, prompt, llm_context, next_run_at)
                       VALUES (%s, %s, %s, %s, %s) RETURNING id""",
                    (target_agent, cron_expr, prompt, llm_ctx, next_run),
                )
                schedule_id = str((await rows.fetchone())["id"])
            result = {"schedule_id": schedule_id, "next_run_at": next_run.isoformat()}
            await _emit_event(
                session_id,
                ToolResultEvent(
                    tool=tool_name,
                    success=True,
                    input=input_data,
                    output=result,
                    harness=harness,
                    skill_name="native",
                ),
            )
            return result

        if tool_name == "native.defer_turn":
            delay = int(input_data.get("delay_seconds", 0))
            info = DeferInfo(delay_seconds=delay)
            _defer_state["info"] = info
            await _emit_event(
                session_id,
                ToolCallEvent(
                    tool=tool_name, input=input_data, harness=harness, skill_name="native"
                ),
            )
            result = {"status": "deferred", "resume_in_seconds": delay}
            await _emit_event(
                session_id,
                ToolResultEvent(
                    tool=tool_name,
                    success=True,
                    input=input_data,
                    output=result,
                    harness=harness,
                    skill_name="native",
                ),
            )
            return result

        if tool_name == "native.defer_turn_until":
            ts_str = input_data.get("iso_timestamp", "")
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError as exc:
                raise RuntimeError(f"invalid iso_timestamp: {ts_str!r}") from exc
            info = DeferInfo(resume_at=ts)
            _defer_state["info"] = info
            await _emit_event(
                session_id,
                ToolCallEvent(
                    tool=tool_name, input=input_data, harness=harness, skill_name="native"
                ),
            )
            result = {"status": "deferred", "resume_at": ts.isoformat()}
            await _emit_event(
                session_id,
                ToolResultEvent(
                    tool=tool_name,
                    success=True,
                    input=input_data,
                    output=result,
                    harness=harness,
                    skill_name="native",
                ),
            )
            return result

        skill_name = tool_snapshot[tool_name].skill_name
        await _emit_event(
            session_id,
            ToolCallEvent(
                tool=tool_name, input=input_data, harness=harness, skill_name=skill_name or None
            ),
        )
        output = await _tool_executor(
            session_id,
            tool_name,
            input_data,
            tool_snapshot,
            auth_context=auth_context,
            agent_name=config.agent_name,
        )
        await _emit_event(
            session_id,
            ToolResultEvent(
                tool=tool_name,
                success=True,
                input=input_data,
                output=output,
                harness=harness,
                skill_name=skill_name or None,
            ),
        )
        return output

    async def emit(event: BaseEvent) -> None:
        await _emit_event(session_id, event)

    await emit(SystemPromptEvent(content=system_prompt, input=history))

    try:
        if harness == "antigravity":
            from omniagent.worker.harness.antigravity import AntigravityAdapter

            adapter = AntigravityAdapter(api_key=llm_api_key)
        elif harness == "claude":
            from omniagent.worker.harness.claude import ClaudeAdapter

            adapter = ClaudeAdapter(api_key=llm_api_key)
        else:
            raise ValueError(f"Unknown harness: {harness!r}")

        result = await adapter.run(
            system_prompt=system_prompt,
            history=history,
            tool_executor=tool_exec,
            emit_event=emit,
            use_monty=use_monty,
            tool_snapshot=tool_snapshot,
            model=model,
        )

        if defer := _defer_state.get("info"):
            await _handle_defer(session_id, result, history, data, defer)
        else:
            await _get_http_client().post(
                f"{CONTROL_PLANE}/internal/sessions/{session_id}/result",
                json={"result": result},
                headers=_internal_headers(),
                timeout=10,
            )

    except Exception as exc:
        logger.exception("run_agent_job failed for session %s", session_id)
        await emit(ErrorEvent(reason=str(exc)))
        raise


async def _handle_defer(
    session_id: str,
    result: str,
    history: list[MessageRecord],
    data: dict[str, Any],
    defer: DeferInfo,
) -> None:
    """Save deferred state and schedule next turn."""
    from omniagent.control_plane.db import get_conn

    now = datetime.now(UTC).isoformat()
    new_history = list(history)
    new_history.append(MessageRecord(role="assistant", content=result, timestamp=now))
    new_history.append(
        MessageRecord(
            role="user", content="[RESUME: Turn resumed. Continue your task.]", timestamp=now
        )
    )

    serialized = json.dumps([m.model_dump() for m in new_history])
    # Carry forward encrypted auth/llm context for the next turn
    deferred_payload = json.dumps(
        {"auth_context": data.get("auth_context"), "llm_context": data.get("llm_context")}
    )

    async with get_conn() as conn:
        await conn.execute(
            "UPDATE sessions SET status='deferred', messages=%s, deferred_payload=%s, updated_at=NOW() WHERE id=%s",
            (serialized, deferred_payload, session_id),
        )

    await _emit_event(session_id, BaseEvent(type="deferred"))

    scheduled_at = defer.scheduled_at()
    await run_agent_job.configure(queue="default", schedule_at=scheduled_at).defer_async(
        session_id=session_id,
        payload=json.dumps(
            {
                "history": [m.model_dump() for m in new_history],
                "auth_context": data.get("auth_context"),
                "llm_context": data.get("llm_context"),
            }
        ),
    )
    logger.info("session %s deferred until %s", session_id, scheduled_at.isoformat())
