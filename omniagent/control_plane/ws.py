"""WebSocket namespace pool and pending-request map."""
import asyncio
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

EXECUTE_TIMEOUT = 30  # seconds

# namespace → list of active WS connections
_namespace_pool: dict[str, list[WebSocket]] = {}
_pool_lock = asyncio.Lock()

# request_id → (future, timeout_handle)
_pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
_pending_lock = asyncio.Lock()

# round-robin counters per namespace
_rr: dict[str, int] = {}


async def register_connection(namespace: str, ws: WebSocket) -> None:
    async with _pool_lock:
        _namespace_pool.setdefault(namespace, []).append(ws)
        logger.info("ws: registered namespace=%s pool_size=%d", namespace, len(_namespace_pool[namespace]))


async def remove_connection(namespace: str, ws: WebSocket) -> None:
    async with _pool_lock:
        pool = _namespace_pool.get(namespace, [])
        if ws in pool:
            pool.remove(ws)
        logger.info("ws: removed namespace=%s pool_size=%d", namespace, len(pool))


async def execute_tool(tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
    namespace = tool_name.split(".")[0]

    async with _pool_lock:
        pool = list(_namespace_pool.get(namespace, []))

    if not pool:
        raise RuntimeError(f"tool_unavailable: no connections for namespace '{namespace}'")

    _rr[namespace] = (_rr.get(namespace, -1) + 1) % len(pool)
    ws = pool[_rr[namespace]]

    request_id = str(uuid.uuid4())
    loop = asyncio.get_event_loop()
    fut: asyncio.Future[dict[str, Any]] = loop.create_future()

    async with _pending_lock:
        _pending[request_id] = fut

    try:
        msg = json.dumps({
            "type": "execute",
            "request_id": request_id,
            "tool_name": tool_name,
            "input": input_data,
        })
        await ws.send_text(msg)

        result = await asyncio.wait_for(fut, timeout=EXECUTE_TIMEOUT)
        return result
    except asyncio.TimeoutError:
        raise RuntimeError(f"tool_timeout: '{tool_name}' did not respond within {EXECUTE_TIMEOUT}s")
    except Exception:
        raise
    finally:
        async with _pending_lock:
            _pending.pop(request_id, None)


async def resolve_pending(request_id: str, result: dict[str, Any]) -> None:
    async with _pending_lock:
        fut = _pending.get(request_id)
    if fut and not fut.done():
        fut.set_result(result)


def get_namespace_pool_info() -> dict[str, int]:
    return {ns: len(conns) for ns, conns in _namespace_pool.items()}
