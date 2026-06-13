"""Procrastinate worker task: run_agent_job."""
import asyncio
import json
import logging
import os
from typing import Any

import httpx
import procrastinate
from procrastinate.contrib.aiopg import AiopgConnector

logger = logging.getLogger(__name__)

CONTROL_PLANE = os.environ.get("OMNIAGENT_CONTROL_PLANE", "http://localhost:8080")
WORKER_SECRET = os.environ.get("OMNIAGENT_WORKER_SECRET", "")

# Shared app instance — used both by worker (to run jobs) and control plane (to defer jobs)
app = procrastinate.App(connector=AiopgConnector(dsn=os.environ.get("DATABASE_URL", "")))


def _headers() -> dict[str, str]:
    return {"X-OmniAgent-Key": WORKER_SECRET}


async def _tool_executor(session_id: str, tool_name: str, input_data: dict) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{CONTROL_PLANE}/internal/tools/execute",
            json={"tool_name": tool_name, "input": input_data, "session_id": session_id},
            headers=_headers(),
            timeout=35,
        )
    if resp.status_code == 503:
        raise RuntimeError(f"tool_unavailable:{tool_name}")
    if resp.status_code == 504:
        raise RuntimeError(f"tool_timeout:{tool_name}")
    resp.raise_for_status()
    return resp.json()["output"]


async def _emit_event(session_id: str, event: dict) -> None:
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{CONTROL_PLANE}/internal/sessions/{session_id}/event",
                json=event,
                headers=_headers(),
                timeout=5,
            )
    except Exception as exc:
        logger.warning("emit_event failed: %s", exc)


def _build_system_prompt(agent_config: dict[str, Any]) -> str:
    lines = [agent_config.get("system_prompt", "")]

    skills = agent_config.get("skills", [])
    if skills:
        lines.append("\nSkills:")
        for skill in skills:
            if skill.get("system_prompt"):
                lines.append(skill["system_prompt"])
            if skill.get("instructions"):
                lines.append(skill["instructions"])

    snapshot = agent_config.get("tool_snapshot", {})
    if snapshot:
        lines.append("\nAvailable tools:")
        for tool_name, schema in snapshot.items():
            lines.append(f"- {tool_name}: {schema.get('description', '')}")
            if schema.get("input_schema"):
                lines.append(f"  Input schema: {json.dumps(schema['input_schema'])}")
            if schema.get("output_schema"):
                lines.append(f"  Output schema: {json.dumps(schema['output_schema'])}")

    return "\n".join(lines)


@app.task(name="run_agent_job", queue="default")
async def run_agent_job(session_id: str, payload: str) -> None:
    data = json.loads(payload)
    agent_config = data["agent_config"]
    history = data.get("history", [])
    llm_api_key = data.get("llm_api_key")

    harness = agent_config["harness"]
    use_monty = agent_config.get("use_monty", False)
    tool_snapshot = agent_config.get("tool_snapshot", {})

    system_prompt = _build_system_prompt(agent_config)

    async def tool_exec(tool_name: str, input_data: dict) -> dict:
        return await _tool_executor(session_id, tool_name, input_data)

    async def emit(event: dict) -> None:
        await _emit_event(session_id, event)

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
        )

        async with httpx.AsyncClient() as client:
            await client.post(
                f"{CONTROL_PLANE}/internal/sessions/{session_id}/result",
                json={"result": result},
                headers=_headers(),
                timeout=10,
            )

    except Exception as exc:
        logger.exception("run_agent_job failed for session %s", session_id)
        # emit(type=error) → control plane marks session "failed" + PG_NOTIFY
        await emit({"type": "error", "reason": str(exc)})
        raise  # let Procrastinate mark job as FAILED in its own table
