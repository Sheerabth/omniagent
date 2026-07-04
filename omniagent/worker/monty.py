"""Monty sandboxed code execution for workers."""

import asyncio
import concurrent.futures
import json
from typing import Any, Protocol

import pydantic_monty as monty_lib

from omniagent.config import settings
from omniagent.worker.models import MontyExecutor, ToolExecutor, ToolSnapshot

_MONTY_WORKERS = settings.monty_executor_workers
_MONTY_TIMEOUT = settings.monty_execution_timeout
_monty_executor: concurrent.futures.ThreadPoolExecutor | None = None


def _get_monty_executor() -> concurrent.futures.ThreadPoolExecutor:
    global _monty_executor
    if _monty_executor is None:
        _monty_executor = concurrent.futures.ThreadPoolExecutor(max_workers=_MONTY_WORKERS)
    return _monty_executor


def make_monty_tool(
    tool_snapshot: dict[str, ToolSnapshot],
    tool_executor: ToolExecutor,
) -> MontyExecutor:
    async def execute_python(code: str, observation: str) -> str:
        result = await run_monty_code(code, tool_snapshot, tool_executor)
        return json.dumps(result)

    return execute_python


async def run_monty_code(
    code: str,
    tool_snapshot: dict[str, ToolSnapshot],
    tool_executor: ToolExecutor,
) -> Any:
    external_fns: dict[str, SyncTool] = {}
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


class SyncTool(Protocol):
    def __call__(self, **kwargs: Any) -> Any: ...


def _make_sync_tool(
    tool_name: str,
    tool_executor: ToolExecutor,
) -> SyncTool:
    def sync_tool(**kwargs: Any) -> Any:
        return asyncio.run(tool_executor(tool_name, kwargs))

    sync_tool.__name__ = tool_name.replace(".", "__").replace("-", "_")
    return sync_tool
