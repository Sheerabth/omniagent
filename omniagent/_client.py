"""omniagent.init() — SDK entry point for user services."""
import asyncio
import json
import logging
import os
import threading
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed

from omniagent._registry import _local_registry

logger = logging.getLogger(__name__)

_config: dict[str, Any] = {}
_ws_task: asyncio.Task | None = None
_loop: asyncio.AbstractEventLoop | None = None


def init(
    *,
    service: str,
    control_plane: str,
    api_key: str,
    namespace: str | None = None,
) -> None:
    """Connect service to OmniAgent control plane and register tools.

    Must be called once at service startup after all @tool definitions are imported.
    """
    global _config, _ws_task, _loop

    _config = {
        "service": service,
        "namespace": namespace or service,
        "control_plane": control_plane,
        "api_key": api_key,
    }

    _loop = asyncio.new_event_loop()
    t = threading.Thread(target=_loop.run_forever, daemon=True)
    t.start()

    _ws_task = asyncio.run_coroutine_threadsafe(_ws_loop(), _loop)
    logger.info("omniagent: connecting to %s as namespace=%s", control_plane, _config["namespace"])


def _build_register_message() -> dict[str, Any]:
    ns = _config["namespace"]
    tools = []
    for fn_name, entry in _local_registry.items():
        tools.append({
            "name": f"{ns}.{fn_name}",
            "description": entry["description"],
            "input_schema": entry["input_schema"],
            "output_schema": entry["output_schema"],
        })
    return {
        "type": "register",
        "service": _config["service"],
        "namespace": ns,
        "tools": tools,
    }


async def _ws_loop() -> None:
    ws_url = _config["control_plane"].replace("http://", "ws://").replace("https://", "wss://")
    ws_url = ws_url.rstrip("/") + "/ws"
    headers = {"X-OmniAgent-Key": _config["api_key"]}

    while True:
        try:
            async with websockets.connect(ws_url, additional_headers=headers) as ws:
                logger.info("omniagent: WS connected, registering %d tools", len(_local_registry))
                await ws.send(json.dumps(_build_register_message()))

                async for raw in ws:
                    msg = json.loads(raw)
                    await _handle_message(ws, msg)

        except ConnectionClosed as e:
            logger.warning("omniagent: WS closed (%s), reconnecting in 5s", e)
        except Exception as e:
            logger.error("omniagent: WS error (%s), reconnecting in 5s", e)

        await asyncio.sleep(5)


async def _handle_message(ws: Any, msg: dict[str, Any]) -> None:
    msg_type = msg.get("type")

    if msg_type == "ping":
        await ws.send(json.dumps({"type": "pong"}))
        return

    if msg_type == "execute":
        request_id = msg["request_id"]
        tool_name = msg["tool_name"]
        input_data = msg["input"]

        ns = _config["namespace"]
        local_name = tool_name.removeprefix(f"{ns}.")

        entry = _local_registry.get(local_name)
        if entry is None:
            await ws.send(json.dumps({
                "type": "execute_result",
                "request_id": request_id,
                "success": False,
                "output": None,
                "error": f"Tool '{tool_name}' not found in local registry",
            }))
            return

        try:
            parsed_input = entry["input"].model_validate(input_data)
            result = await entry["fn"](parsed_input)
            await ws.send(json.dumps({
                "type": "execute_result",
                "request_id": request_id,
                "success": True,
                "output": result.model_dump(),
                "error": None,
            }))
        except Exception as exc:
            await ws.send(json.dumps({
                "type": "execute_result",
                "request_id": request_id,
                "success": False,
                "output": None,
                "error": str(exc),
            }))
