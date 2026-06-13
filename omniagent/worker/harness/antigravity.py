"""Antigravity (Gemini) harness adapter."""

import inspect
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from google.antigravity import Agent

try:
    from google.antigravity.connections.local.local_connection_config import LocalAgentConfig
    from google.antigravity.hooks import policy
except ImportError as exc:
    raise ImportError(
        f"google-antigravity internal API mismatch: {exc}. "
        "Verify LocalAgentConfig and policy import paths for the installed package version."
    ) from exc

from omniagent.worker.harness.base import EXECUTE_PYTHON_DESCRIPTION, HarnessAdapter

logger = logging.getLogger(__name__)

_TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool}


class AntigravityAdapter(HarnessAdapter):

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key

    async def run(
        self,
        system_prompt: str,
        history: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[dict]],
        emit_event: Callable[[dict], Awaitable[None]],
        use_monty: bool,
        tool_snapshot: dict[str, Any],
        model: str = "",
    ) -> str:
        if use_monty:
            tools = [self._build_monty_tool(tool_snapshot, tool_executor, emit_event)]
        else:
            tools = self._build_tool_callables(tool_snapshot, tool_executor, emit_event)

        full_system = _build_system_with_history(system_prompt, history)

        config = LocalAgentConfig(
            system_instructions=full_system,
            tools=tools,
            policies=[policy.allow_all()],
            api_key=self._api_key,
            workspaces=[],
            model=model or "gemini-2.5-flash",
        )

        latest_user = next(
            (m["content"] for m in reversed(history) if m.get("role") == "user"),
            "",
        )

        await emit_event({"type": "thinking", "content": "Starting Antigravity agent"})

        async with Agent(config) as agent:
            response = await agent.chat(latest_user)
            result = await _extract_text(response)

        return result

    def _build_tool_callables(
        self,
        tool_snapshot: dict[str, Any],
        tool_executor: Callable[[str, dict], Awaitable[dict]],
        emit_event: Callable[[dict], Awaitable[None]],
    ) -> list[Callable]:
        return [
            _make_tool_fn(name, schema, tool_executor, emit_event)
            for name, schema in tool_snapshot.items()
        ]

    def _build_monty_tool(
        self,
        tool_snapshot: dict[str, Any],
        tool_executor: Callable[[str, dict], Awaitable[dict]],
        emit_event: Callable[[dict], Awaitable[None]],
    ) -> Callable:
        from omniagent.worker.monty import make_monty_tool

        inner = make_monty_tool(tool_snapshot, tool_executor)

        async def execute_python(code: str, observation: str) -> str:
            await emit_event(
                {
                    "type": "tool_call",
                    "tool": "execute_python",
                    "input": {"code": code, "observation": observation},
                }
            )
            try:
                result = await inner(code=code, observation=observation)
                await emit_event({"type": "tool_result", "tool": "execute_python", "success": True})
                return result
            except Exception:
                await emit_event(
                    {"type": "tool_result", "tool": "execute_python", "success": False}
                )
                raise

        execute_python.__name__ = "execute_python"
        execute_python.__doc__ = EXECUTE_PYTHON_DESCRIPTION
        return execute_python


def _make_tool_fn(
    tool_name: str,
    schema: dict[str, Any],
    tool_executor: Callable[[str, dict], Awaitable[dict]],
    emit_event: Callable[[dict], Awaitable[None]],
) -> Callable:
    input_schema = schema.get("input_schema", {})
    props = input_schema.get("properties", {})
    param_names = [k for k in props if k != "observation"]

    async def tool_fn(**kwargs: Any) -> str:
        input_data = dict(kwargs)
        await emit_event({"type": "tool_call", "tool": tool_name, "input": input_data})
        try:
            output = await tool_executor(tool_name, input_data)
            return json.dumps(output)
        except Exception as exc:
            err = str(exc)
            await emit_event(
                {"type": "tool_result", "tool": tool_name, "success": False, "error": err}
            )
            return json.dumps({"error": err})

    params = [
        inspect.Parameter("observation", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
    ]
    for p in param_names:
        ann = _TYPE_MAP.get(props[p].get("type", "string"), str)
        params.append(inspect.Parameter(p, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann))

    tool_fn.__signature__ = inspect.Signature(params)
    tool_fn.__name__ = _safe_name(tool_name)
    tool_fn.__doc__ = schema.get("description", "")
    return tool_fn


def _safe_name(tool_name: str) -> str:
    return tool_name.replace(".", "__").replace("-", "_")


def _build_system_with_history(system_prompt: str, history: list[dict]) -> str:
    prior = [m for m in history[:-1] if m.get("role") in ("user", "assistant")]
    if not prior:
        return system_prompt
    transcript = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in prior)
    return f"{system_prompt}\n\n--- Prior conversation ---\n{transcript}\n--- End prior conversation ---"


async def _extract_text(response: Any) -> str:
    if hasattr(response, "text") and callable(response.text):
        return await response.text()
    if hasattr(response, "content"):
        c = response.content
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(getattr(part, "text", str(part)) for part in c)
    return str(response)
