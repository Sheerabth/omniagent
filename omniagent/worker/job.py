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
from langfuse import Langfuse
from procrastinate import PsycopgConnector

from omniagent.api.crypto import decrypt_auth_context
from omniagent.api.models import MessageRecord, ToolCallEntry
from omniagent.config import settings
from omniagent.logging_config import trace_id_var
from omniagent.worker.models import (
    BaseEvent,
    ErrorEvent,
    SessionConfig,
    SystemPromptEvent,
    ThinkingEvent,
    ToolboxSnapshot,
    ToolCallEvent,
    ToolResultEvent,
    ToolSnapshot,
)
from omniagent.worker.native import NATIVE_TOOL_DESCRIPTIONS, NATIVE_TOOL_SCHEMAS, DeferInfo

logger = logging.getLogger(__name__)

# ponytail: no-op if LANGFUSE_SECRET_KEY not set — no config change needed
# for deployments that don't run langfuse.
_langfuse = Langfuse() if settings.langfuse_secret_key else None

_DEFAULT_TOOL_TIMEOUT = settings.tool_execution_timeout
_CANCEL_MARKER = "[CANCELLED: previous response was stopped by the user before completing]"

app = procrastinate.App(connector=PsycopgConnector(conninfo=settings.database_url))

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


async def _fetch_session_config(session_id: str) -> SessionConfig:
    from omniagent.api.db import get_conn

    async with get_conn() as conn:
        rows = await conn.execute(
            "SELECT agent_name, agent_version, toolbox_versions, tool_refs FROM sessions WHERE id = %s",
            (session_id,),
        )
        session = await rows.fetchone()
        if not session:
            raise RuntimeError(f"session_not_found:{session_id}")

        agent_name = session["agent_name"]
        agent_version = session["agent_version"]
        toolbox_versions: dict[str, str] = session["toolbox_versions"] or {}
        direct_tool_refs: list[str] = session["tool_refs"] or []

        rows = await conn.execute(
            "SELECT * FROM agents WHERE name = %s AND version = %s",
            (agent_name, agent_version),
        )
        agent = await rows.fetchone()
        if not agent:
            raise RuntimeError(f"agent_version_deleted:{agent_name}:{agent_version}")

        # Load toolboxes
        toolboxes: list[ToolboxSnapshot] = []
        toolbox_tool_names: list[str] = []
        tool_to_toolbox: dict[str, str] = {}
        for tname, toolbox_version in toolbox_versions.items():
            rows = await conn.execute(
                "SELECT * FROM toolboxes WHERE name = %s AND version = %s",
                (tname, toolbox_version),
            )
            toolbox = await rows.fetchone()
            if not toolbox:
                raise RuntimeError(f"toolbox_version_deleted:{tname}:{toolbox_version}")
            toolboxes.append(ToolboxSnapshot(system_prompt=toolbox["system_prompt"] or ""))
            for t in toolbox["tool_names"]:
                toolbox_tool_names.append(t)
                tool_to_toolbox[t] = tname

        # Batch-load all tools needed
        all_tool_names = list(set(toolbox_tool_names + direct_tool_refs))
        tool_rows: dict[str, Any] = {}
        if all_tool_names:
            rows = await conn.execute("SELECT * FROM tools WHERE name = ANY(%s)", (all_tool_names,))
            for tool in await rows.fetchall():
                tool_rows[tool["name"]] = tool

        # Batch-fetch auth by (namespace, scheme_name) pairs
        ns_scheme_pairs = list(
            {
                (t["namespace"], (t["openapi_security"] or {}).get("scheme_name", ""))
                for t in tool_rows.values()
                if t.get("namespace") and t.get("openapi_security")
            }
        )
        ns_auth: dict[tuple[str, str], Any] = {}
        if ns_scheme_pairs:
            namespaces = list({p[0] for p in ns_scheme_pairs})
            rows = await conn.execute(
                "SELECT namespace, scheme_name, auth_context FROM namespace_auth WHERE namespace = ANY(%s)",
                (namespaces,),
            )
            ns_scheme_set = set(ns_scheme_pairs)
            for r in await rows.fetchall():
                pair = (r["namespace"], r["scheme_name"])
                if pair in ns_scheme_set:
                    ns_auth[pair] = decrypt_auth_context(r["auth_context"])

        # Build tool_snapshot — toolbox tools first (take precedence over direct refs)
        tool_snapshot: dict[str, ToolSnapshot] = {}
        for name in [*toolbox_tool_names, *direct_tool_refs]:
            if name in tool_rows and name not in tool_snapshot:
                tool = tool_rows[name]
                tool_snapshot[name] = ToolSnapshot(
                    name=tool["name"],
                    description=tool["description"],
                    input_schema=tool["input_schema"],
                    output_schema=tool["output_schema"],
                    openapi_method=tool["openapi_method"],
                    openapi_path=tool["openapi_path"],
                    openapi_base_url=tool["openapi_base_url"],
                    openapi_security=tool["openapi_security"],
                    timeout=tool["timeout"],
                    skill_name=tool_to_toolbox.get(name, ""),
                    auth_context=ns_auth.get(
                        (tool["namespace"], (tool["openapi_security"] or {}).get("scheme_name", ""))
                    ),
                )

    return SessionConfig(
        agent_name=agent_name,
        harness=agent["harness"],
        model=agent["model"],
        system_prompt=agent["system_prompt"],
        use_monty=agent["use_monty"],
        toolboxes=toolboxes,
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

    from omniagent.api.db import get_conn

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


_CH = lambda sid: "session_" + sid.replace("-", "_")  # noqa: E731


async def _emit_event(session_id: str, event: BaseEvent) -> None:
    from omniagent.api.db import get_conn

    ch = _CH(session_id)
    try:
        if event.type == "error":
            async with get_conn() as conn:
                await conn.execute(
                    "UPDATE sessions SET status='failed', updated_at=NOW() WHERE id=%s AND status='running'",
                    (session_id,),
                )
                await conn.execute("SELECT pg_notify(%s, %s)", (ch, "error"))
        elif event.type == "tool_result":
            ev = event.model_dump(exclude_none=True)
            if ev.get("input") is not None and "output" in ev:
                entry_json = json.dumps(
                    ToolCallEntry(
                        tool_name=ev.get("tool") or "",
                        input=ev.get("input") or {},
                        output=ev.get("output"),
                        harness=ev.get("harness"),
                        skill_name=ev.get("skill_name"),
                        timestamp=datetime.now(UTC),
                        success=ev.get("success", True),
                        error=ev.get("error"),
                    ).model_dump(mode="json")
                )
                async with get_conn() as conn:
                    await conn.execute(
                        "UPDATE sessions SET tool_calls = tool_calls || %s::jsonb WHERE id=%s",
                        (f"[{entry_json}]", session_id),
                    )
                    await conn.execute("SELECT pg_notify(%s, %s)", (ch, "update"))
        else:
            async with get_conn() as conn:
                await conn.execute("SELECT pg_notify(%s, %s)", (ch, event.type))
    except Exception as exc:
        logger.warning("emit_event failed: %s", exc)


async def _complete_session(session_id: str, result: str, prior_count: int) -> None:
    """Append the assistant reply and decide whether to go idle or chain another turn.

    `prior_count` is the message count this turn started with. If the array
    has grown beyond that (via /run appending while this turn was in flight),
    there's unanswered input queued — go back to 'pending' and schedule an
    immediate follow-up turn instead of 'idle', so nothing sent while busy
    gets silently dropped. `status` is job-owned end to end: only this
    function (or _handle_defer) ever writes it, always a confirmed fact, so
    a fresh SSE listener can always trust it immediately, cancelled included.

    If cancellation was requested while this turn was in flight, this turn's
    own answer is stale and gets discarded — that only stops THIS turn, same
    as "stop generating" in a chat UI. Anything queued in the meantime still
    gets picked up by an immediate follow-up turn, exactly like the non
    cancelled catch-up path below.
    """
    from omniagent.api.db import get_conn

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
            await conn.execute(
                "UPDATE sessions SET status=%s, "
                "messages = jsonb_insert(messages, %s::text[], %s::jsonb), "
                "cancel_requested=false, updated_at=NOW() WHERE id=%s",
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
            await conn.execute(
                "UPDATE sessions SET status=%s, "
                "messages = messages || %s::jsonb, "
                "updated_at=NOW() WHERE id=%s",
                (next_status, f"[{assistant_json}]", session_id),
            )
            await conn.execute("SELECT pg_notify(%s, %s)", (ch, "complete"))

    if has_queued_input:
        await run_agent_job.configure(queue="default").defer_async(session_id=session_id)
        logger.info("session %s has queued input, scheduling follow-up turn", session_id)


def _safe_tool_name(name: str) -> str:
    return name.replace(".", "__").replace("-", "_")


def _build_system_prompt(
    config: SessionConfig,
    extra_tools: dict[str, ToolSnapshot] | None = None,
) -> str:
    lines = [config.system_prompt]

    for toolbox in config.toolboxes:
        if toolbox.system_prompt:
            lines.append(toolbox.system_prompt)

    effective_snapshot = {**config.tool_snapshot, **(extra_tools or {})}

    if effective_snapshot and not config.use_monty:
        # Group tools by toolbox for clearer LLM context.
        by_toolbox: dict[str, list[tuple[str, ToolSnapshot]]] = {}
        for tool_name, schema in effective_snapshot.items():
            by_toolbox.setdefault(schema.skill_name or "", []).append((tool_name, schema))
        for skill_name, tools in by_toolbox.items():
            lines.append(f"\n## Toolbox: {skill_name}" if skill_name else "\nAvailable tools:")
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
async def run_agent_job(session_id: str) -> None:
    trace_id_var.set(session_id)
    job_start = time.monotonic()
    from omniagent.api.db import get_conn

    async with get_conn() as conn:
        row = await (
            await conn.execute("SELECT status, messages FROM sessions WHERE id=%s", (session_id,))
        ).fetchone()
        if not row:
            logger.warning("run_agent_job: session %s not found, skipping", session_id)
            return
        if row["status"] == "cancelled":
            logger.info("run_agent_job: session %s cancelled, skipping", session_id)
            return
        history = [MessageRecord.model_validate(m) for m in (row["messages"] or [])]
        if row["status"] in ("pending", "deferred"):
            await conn.execute(
                "UPDATE sessions SET status='running', updated_at=NOW() WHERE id=%s",
                (session_id,),
            )
            await _emit_event(session_id, BaseEvent(type="running"))

    config = await _fetch_session_config(session_id)
    harness = config.harness
    model = config.model
    use_monty = config.use_monty

    # Inject native tools — must happen before building system prompt
    native_tools = {name: _make_native_tool_snapshot(name) for name in NATIVE_TOOL_DESCRIPTIONS}
    tool_snapshot = {**config.tool_snapshot, **native_tools}
    system_prompt = _build_system_prompt(config, extra_tools=native_tools)

    llm_api_key = os.environ.get(f"OMNIAGENT_{harness.upper()}_API_KEY")

    # Shared defer state — set by native.defer_turn / native.defer_turn_until inside tool_exec
    _defer_state: dict[str, DeferInfo] = {}

    # Langfuse trace — wraps the entire turn with nested generations and spans.
    last_user = history[-1].content if history and history[-1].role == "user" else None
    trace = (
        _langfuse.trace(  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
            name=config.agent_name,
            session_id=session_id,
            user_id=config.agent_name,
            metadata={"harness": harness, "model": model, "monty": use_monty},
            input=last_user,
        )
        if _langfuse
        else None
    )

    async def _do_tool_exec(tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        # Native tools are handled here, not by the HTTP executor
        if tool_name in (
            "native.memory_get",
            "native.memory_set",
            "native.memory_delete",
            "native.memory_list",
        ):
            from omniagent.api.db import get_conn

            await _emit_event(
                session_id,
                ToolCallEvent(
                    tool=tool_name, input=input_data, harness=harness, skill_name="native"
                ),
            )
            async with get_conn() as conn:
                result: Any = None
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
            from omniagent.api.db import get_conn

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
                result: Any = [
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

            from omniagent.api.db import get_conn

            cron_expr = input_data.get("cron_expr", "")
            prompt = input_data.get("prompt", "")
            target_agent = config.agent_name
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
                    """INSERT INTO schedules (agent_name, cron_expr, prompt, next_run_at)
                       VALUES (%s, %s, %s, %s) RETURNING id""",
                    (target_agent, cron_expr, prompt, next_run),
                )
                schedule_row = await rows.fetchone()
                assert schedule_row is not None, "INSERT RETURNING returned no row"
                schedule_id = str(schedule_row["id"])
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

        if tool_name == "native.schedule_update":
            from croniter import croniter as _croniter

            from omniagent.api.db import get_conn

            schedule_id = input_data.get("schedule_id")
            cron_expr = input_data.get("cron_expr")
            prompt = input_data.get("prompt")
            await _emit_event(
                session_id,
                ToolCallEvent(
                    tool=tool_name, input=input_data, harness=harness, skill_name="native"
                ),
            )
            async with get_conn() as conn:
                row = await (
                    await conn.execute(
                        "SELECT cron_expr, prompt FROM schedules WHERE id=%s AND agent_name=%s",
                        (schedule_id, config.agent_name),
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
                    (new_cron, new_prompt, next_run, schedule_id, config.agent_name),
                )
            result = {
                "schedule_id": schedule_id,
                "cron_expr": new_cron,
                "next_run_at": next_run.isoformat(),
            }
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

        if tool_name == "native.schedule_delete":
            from omniagent.api.db import get_conn

            schedule_id = input_data.get("schedule_id")
            await _emit_event(
                session_id,
                ToolCallEvent(
                    tool=tool_name, input=input_data, harness=harness, skill_name="native"
                ),
            )
            async with get_conn() as conn, conn.transaction():
                await conn.execute(
                    "UPDATE sessions SET status='cancelled', updated_at=NOW() WHERE schedule_id=%s AND status='pending'",
                    (schedule_id,),
                )
                res = await conn.execute(
                    "DELETE FROM schedules WHERE id=%s AND agent_name=%s",
                    (schedule_id, config.agent_name),
                )
                if res.rowcount == 0:
                    raise RuntimeError(f"schedule {schedule_id!r} not found")
            result = {"schedule_id": schedule_id, "deleted": True}
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
            _agent_name=config.agent_name,
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

    async def tool_exec(tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        _lf_span = trace.span(name=tool_name, input=input_data) if trace else None
        result = await _do_tool_exec(tool_name, input_data)
        if _lf_span:
            _lf_span.update(output=result).end()
        return result

    async def emit(event: BaseEvent) -> None:
        if trace and isinstance(event, ThinkingEvent):
            trace.span(name="thinking", input=event.content[:200] if event.content else None).end()
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

        generation = (
            trace.generation(
                name=f"{harness}/{model}",
                model=model,
                input=last_user,
            )
            if trace
            else None
        )
        result = await adapter.run(
            system_prompt=system_prompt,
            history=history,
            tool_executor=tool_exec,
            emit_event=emit,
            use_monty=use_monty,
            tool_snapshot=tool_snapshot,
            model=model,
        )
        if generation:
            generation.end(output=result)
        if trace:
            trace.update(output=result)

        if defer := _defer_state.get("info"):
            await _handle_defer(session_id, result, history, defer)
            outcome = "deferred"
        else:
            await _complete_session(session_id, result, len(history))
            outcome = "completed"
        logger.info(
            "run_agent_job finished",
            extra={
                "session_id": session_id,
                "outcome": outcome,
                "duration_ms": round((time.monotonic() - job_start) * 1000),
            },
        )

    except Exception as exc:
        logger.exception(
            "run_agent_job failed for session %s",
            session_id,
            extra={"duration_ms": round((time.monotonic() - job_start) * 1000)},
        )
        await emit(ErrorEvent(reason=str(exc)))
        raise


async def _handle_defer(
    session_id: str,
    result: str,
    history: list[MessageRecord],
    defer: DeferInfo,
) -> None:
    """Persist the deferred turn's outcome and re-arm the session for wake-up.

    Re-fetches messages fresh (under FOR UPDATE) instead of overwriting from
    the stale `history` snapshot — anything appended via /run while this turn
    was in flight must survive, not get silently erased by this write.

    If cancellation was requested mid-turn, its decision to defer is
    discarded — same as _complete_session, cancel only stops this turn, not
    the conversation. Any messages queued in the meantime get an immediate
    follow-up turn instead of waiting for the (now-discarded) defer's
    wake-up time.
    """
    from omniagent.api.db import get_conn

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
            await conn.execute(
                "UPDATE sessions SET status=%s, "
                "messages = jsonb_insert(messages, %s::text[], %s::jsonb), "
                "cancel_requested=false, updated_at=NOW() WHERE id=%s",
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
            await run_agent_job.configure(queue="default").defer_async(session_id=session_id)
        return

    await _emit_event(session_id, BaseEvent(type="deferred"))

    scheduled_at_iso = defer.scheduled_at()
    scheduled_at_dt = datetime.fromisoformat(scheduled_at_iso)
    await run_agent_job.configure(queue="default", schedule_at=scheduled_at_dt).defer_async(
        session_id=session_id,
    )
    logger.info("session %s deferred until %s", session_id, scheduled_at_iso)
