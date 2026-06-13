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
    """Create a Python callable 'execute_python' tool backed by Monty."""

    async def execute_python(observation: str, code: str) -> str:
        """Execute Python code in a sandboxed environment."""
        result = await run_monty_code(code, tool_snapshot, tool_executor)
        return json.dumps(result)

    return execute_python


async def run_monty_code(
    code: str,
    tool_snapshot: dict[str, Any],
    tool_executor: Callable[[str, dict], Awaitable[dict]],
) -> Any:
    """Execute code string in Monty sandbox with OmniAgent tools as external_functions."""
    external_fns: dict[str, Callable] = {}

    for tool_name in tool_snapshot:
        safe_name = tool_name.replace(".", "__").replace("-", "_")
        # Build a sync wrapper that runs the async tool_executor
        external_fns[safe_name] = _make_sync_tool(tool_name, tool_executor)

    monty = monty_lib.Monty(code)
    # run_async returns an awaitable when external_functions are async-capable
    result = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: monty.run(external_functions=external_fns),
    )
    return result


def _make_sync_tool(
    tool_name: str,
    tool_executor: Callable[[str, dict], Awaitable[dict]],
) -> Callable:
    """Sync wrapper around async tool_executor for use inside Monty."""

    def sync_tool(**kwargs: Any) -> Any:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(tool_executor(tool_name, kwargs))
        finally:
            loop.close()

    sync_tool.__name__ = tool_name.replace(".", "__").replace("-", "_")
    return sync_tool
