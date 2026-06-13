"""Monty sandboxed code execution for workers."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pydantic_monty as monty_lib


def make_monty_tool(
    tool_snapshot: dict[str, Any],
    tool_executor: Callable[[str, dict], Awaitable[dict]],
) -> Callable:
    async def execute_python(code: str, observation: str) -> str:
        result = await run_monty_code(code, tool_snapshot, tool_executor)
        return json.dumps(result)

    return execute_python


async def run_monty_code(
    code: str,
    tool_snapshot: dict[str, Any],
    tool_executor: Callable[[str, dict], Awaitable[dict]],
) -> Any:
    external_fns: dict[str, Callable] = {}
    for tool_name in tool_snapshot:
        safe_name = tool_name.replace(".", "__").replace("-", "_")
        external_fns[safe_name] = _make_sync_tool(tool_name, tool_executor)

    monty = monty_lib.Monty(code)
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: monty.run(external_functions=external_fns),
    )
    return result


def _make_sync_tool(
    tool_name: str,
    tool_executor: Callable[[str, dict], Awaitable[dict]],
) -> Callable:
    def sync_tool(**kwargs: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(tool_executor(tool_name, kwargs))
        finally:
            loop.close()

    sync_tool.__name__ = tool_name.replace(".", "__").replace("-", "_")
    return sync_tool
