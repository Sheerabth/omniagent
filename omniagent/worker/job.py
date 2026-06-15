"""Procrastinate worker task: run_agent_job."""

import asyncio
import json
import logging
import os
import time
from typing import Any

import httpx
import jwt
import procrastinate
from procrastinate import PsycopgConnector

logger = logging.getLogger(__name__)

CONTROL_PLANE = os.environ.get("OMNIAGENT_CONTROL_PLANE", "http://localhost:8080")
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


def _make_assertion(session_id: str, tool_name: str) -> str:
    """Mint a short-lived JWT assertion for worker → service auth.

    Services verify this with verify_worker_assertion() using the same INTERNAL_KEY.
    """
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "omniagent-worker",
            "session_id": session_id,
            "tool": tool_name,
            "iat": now,
            "exp": now + 60,
        },
        INTERNAL_KEY,
        algorithm="HS256",
    )


def _worker_service_headers(session_id: str, tool_name: str) -> dict[str, str]:
    """Headers the worker sends to external services on /execute."""
    return {
        "X-OmniAgent-Assertion": _make_assertion(session_id, tool_name),
        "X-OmniAgent-Session-Id": session_id,
    }


async def _fetch_session_config(session_id: str) -> dict[str, Any]:
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

        skills = []
        all_tool_names: list[str] = []
        for skill_name, skill_version in skill_versions.items():
            rows = await conn.execute(
                "SELECT * FROM skills WHERE name = %s AND version = %s",
                (skill_name, skill_version),
            )
            skill = await rows.fetchone()
            if not skill:
                raise RuntimeError(f"skill_version_deleted:{skill_name}:{skill_version}")
            skills.append(dict(skill))
            all_tool_names.extend(skill["tool_names"])

        tool_snapshot: dict[str, Any] = {}
        if all_tool_names:
            rows = await conn.execute("SELECT * FROM tools WHERE name = ANY(%s)", (all_tool_names,))
            for tool in await rows.fetchall():
                tool_snapshot[tool["name"]] = {
                    "name": tool["name"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"],
                    "output_schema": tool["output_schema"],
                    "execute_url": tool["execute_url"] or "",
                }

    return {
        "harness": agent["harness"],
        "model": agent["model"],
        "system_prompt": agent["system_prompt"],
        "use_monty": agent["use_monty"],
        "auth_context": agent.get("auth_context"),
        "skills": skills,
        "tool_snapshot": tool_snapshot,
    }


async def _tool_executor(
    session_id: str,
    tool_name: str,
    input_data: dict,
    tool_snapshot: dict[str, Any],
    auth_context: Any = None,
) -> dict:
    tool = tool_snapshot.get(tool_name)
    if not tool:
        raise RuntimeError(f"tool_not_found:{tool_name}")

    execute_url = tool.get("execute_url", "")
    if not execute_url:
        raise RuntimeError(f"tool_no_execute_url:{tool_name}")

    local_name = tool_name.split(".", 1)[-1] if "." in tool_name else tool_name

    body: dict[str, Any] = {"tool": local_name, "input": input_data}
    if auth_context is not None:
        body["auth_context"] = auth_context
        body["session_id"] = session_id

    resp = await _get_http_client().post(
        execute_url,
        json=body,
        headers=_worker_service_headers(session_id, tool_name),
        timeout=35,
    )

    if resp.status_code == 404:
        raise RuntimeError(f"tool_unavailable:{tool_name}")
    resp.raise_for_status()
    return resp.json()["output"]


async def _emit_event(session_id: str, event: dict) -> None:
    try:
        await _get_http_client().post(
            f"{CONTROL_PLANE}/internal/sessions/{session_id}/event",
            json=event,
            headers=_internal_headers(),
            timeout=5,
        )
    except Exception as exc:
        logger.warning("emit_event failed: %s", exc)


def _safe_tool_name(name: str) -> str:
    return name.replace(".", "__").replace("-", "_")


def _build_system_prompt(config: dict[str, Any], llm_context: dict[str, Any] | None = None) -> str:
    lines = [config.get("system_prompt", "")]
    if llm_context:
        lines.append(f"\nUser context: {json.dumps(llm_context, default=str)}")

    for skill in config.get("skills", []):
        if skill.get("system_prompt"):
            lines.append(skill["system_prompt"])
        if skill.get("instructions"):
            lines.append(skill["instructions"])

    tool_snapshot = config.get("tool_snapshot", {})
    use_monty = config.get("use_monty", False)

    if tool_snapshot and not use_monty:
        lines.append("\nAvailable tools:")
        for tool_name, schema in tool_snapshot.items():
            lines.append(f"- {tool_name}: {schema.get('description', '')}")
            if schema.get("input_schema"):
                lines.append(f"  Input schema: {json.dumps(schema['input_schema'])}")
            if schema.get("output_schema"):
                lines.append(f"  Output schema: {json.dumps(schema['output_schema'])}")

    if tool_snapshot and use_monty:
        lines.append(
            "\nYou have access to one tool: `execute_python`. "
            "You MUST use it to interact with all external capabilities. "
            "ALWAYS write a SINGLE execute_python call that does everything — fetch all data, process it, and return the final answer. "
            "Never split work across multiple execute_python calls. "
            "Write Python code and call the following functions (available as globals inside the sandbox):"
        )
        for tool_name, schema in tool_snapshot.items():
            fn = _safe_tool_name(tool_name)
            props = schema.get("input_schema", {}).get("properties", {})
            params = ", ".join(
                f"{k}=..." for k in props if k not in ("observation", "auth_context", "llm_context")
            )
            out_schema = schema.get("output_schema") or {}
            out_props = out_schema.get("properties", out_schema)
            out_info = json.dumps(out_props) if out_props else "dict"
            lines.append(f"- {fn}({params})  -> {out_info}  # {schema.get('description', '')}")
        lines.append(
            "\nIMPORTANT: These functions return Python dicts DIRECTLY — there is NO 'result' wrapper key. "
            "Access fields directly, e.g.: `data = some_fn(x=1); value = data['field_name']`\n"
            "Return a value from your code — the LAST EXPRESSION is the result. "
            "Do NOT use print() to output results — print() returns None and the tool will return null. "
            "Build a result variable and put it as the final line.\n"
            "ALWAYS include both `observation` (what you're doing) and `code` (the Python). "
            "Example: execute_python(observation='get Tokyo weather', code='result = get_weather(city=\"Tokyo\")\\nresult')"
        )

    return "\n".join(lines)


@app.task(name="run_agent_job", queue="default")
async def run_agent_job(session_id: str, payload: str) -> None:
    data = json.loads(payload)
    history = data.get("history", [])
    runtime_auth_context: Any = data.get("auth_context")
    runtime_llm_context: Any = data.get("llm_context")

    config = await _fetch_session_config(session_id)
    harness = config["harness"]
    model = config["model"]
    use_monty = config["use_monty"]
    tool_snapshot = config["tool_snapshot"]

    # Runtime auth_context replaces agent default wholesale — no merge.
    auth_context = (
        runtime_auth_context if runtime_auth_context is not None else config.get("auth_context")
    )

    system_prompt = _build_system_prompt(config, runtime_llm_context)

    llm_api_key = os.environ.get(f"OMNIAGENT_{harness.upper()}_API_KEY")

    async def tool_exec(tool_name: str, input_data: dict) -> dict:
        await _emit_event(
            session_id,
            {
                "type": "tool_call",
                "tool": tool_name,
                "input": input_data,
                "harness": harness,
            },
        )
        output = await _tool_executor(
            session_id,
            tool_name,
            input_data,
            tool_snapshot,
            auth_context=auth_context,
        )
        await _emit_event(
            session_id,
            {
                "type": "tool_result",
                "tool": tool_name,
                "success": True,
                "input": input_data,
                "output": output,
                "harness": harness,
            },
        )
        return output

    async def emit(event: dict) -> None:
        await _emit_event(session_id, event)

    await emit(
        {
            "type": "system_prompt",
            "content": system_prompt,
            "input": history,
        }
    )

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

        await _get_http_client().post(
            f"{CONTROL_PLANE}/internal/sessions/{session_id}/result",
            json={"result": result},
            headers=_internal_headers(),
            timeout=10,
        )

    except Exception as exc:
        logger.exception("run_agent_job failed for session %s", session_id)
        await emit({"type": "error", "reason": str(exc)})
        raise
