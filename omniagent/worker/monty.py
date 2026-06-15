"""Monty sandboxed code execution for workers."""

import asyncio
import concurrent.futures
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

import pydantic_monty as monty_lib

from omniagent.worker.models import ToolSnapshot

_MONTY_WORKERS = int(os.environ.get("MONTY_EXECUTOR_WORKERS", "4"))
_MONTY_TIMEOUT = int(os.environ.get("MONTY_EXECUTION_TIMEOUT", "30"))
_monty_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _get_monty_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _monty_executor
    if _monty_executor is None:
        _monty_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_MONTY_WORKERS)
    return _monty_executor


def make_monty_tool(
    tool_snapshot: dict[str, ToolSnapshot],
    tool_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[..., Awaitable[str]]:
    async def execute_python(code: str, observation: str) -> str:
        result = await run_monty_code(code, tool_snapshot, tool_executor)
        return json.dumps(result)

    return execute_python


async def run_monty_code(
    code: str,
    tool_snapshot: dict[str, ToolSnapshot],
    tool_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Any:
    external_fns: dict[str, Callable] = {}
    for tool_name in tool_snapshot:
        safe_name = tool_name.replace(".", "__").replace("-", "_")
        external_fns[safe_name] = _make_sync_tool(tool_name, tool_executor)

    monty = monty_lib.Monty(code)
    # Run Monty in a thread pool with a timeout.  If the timeout fires the
    # coroutine raises TimeoutError, but the thread itself keeps running —
    # ThreadPoolExecutor threads cannot be interrupted.  A hung thread ties up
    # one executor worker until the process restarts.  Mitigation: increase
    # MONTY_EXECUTOR_WORKERS to tolerate hung threads, or move Monty to a
    # subprocess with hard-kill in the future.
    result = await asyncio.wait_for(
        asyncio.get_running_loop().run_in_executor(
            _get_monty_executor(),
            lambda: monty.run(external_functions=external_fns),
        ),
        timeout=_MONTY_TIMEOUT,
    )
    return result


def _make_sync_tool(
    tool_name: str,
    tool_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
) -> Callable[..., Any]:
    def sync_tool(**kwargs: Any) -> Any:
        return asyncio.run(tool_executor(tool_name, kwargs))

    sync_tool.__name__ = tool_name.replace(".", "__").replace("-", "_")
    return sync_tool
