"""omniagent SDK — library-style, framework-agnostic."""

import logging
from typing import Any

import httpx

from omniagent._registry import _local_registry

logger = logging.getLogger(__name__)

_config: dict[str, Any] = {}


def init(
    *,
    service: str,
    control_plane: str,
    api_key: str,
    execute_url: str,
    namespace: str | None = None,
) -> None:
    """Register tools with the control plane. Call once at startup.

    execute_url: full URL the worker will POST tool calls to
                 (e.g. "http://localhost:8001/execute" or "http://svc.internal/api/v1/omniagent/execute").
                 Your app must handle POST requests at this URL.
    """
    global _config

    ns = namespace or service
    _config = {
        "service": service,
        "namespace": ns,
        "control_plane": control_plane,
        "api_key": api_key,
    }

    tools = [
        {
            "name": f"{ns}.{fn_name}",
            "description": entry["description"],
            "input_schema": entry["input_schema"],
            "output_schema": entry["output_schema"],
        }
        for fn_name, entry in _local_registry.items()
    ]

    resp = httpx.post(
        f"{control_plane}/tools/register",
        json={"namespace": ns, "service": service, "execute_url": execute_url, "tools": tools},
        headers={"X-OmniAgent-Key": api_key},
    )
    resp.raise_for_status()
    logger.info("omniagent: registered %d tools at %s", len(tools), execute_url)


async def handle_execute(tool: str, input: dict) -> dict:
    """Call from your /execute route handler."""
    entry = _local_registry.get(tool)
    if entry is None:
        raise KeyError(f"Tool '{tool}' not found")
    parsed = entry["input"].model_validate(input)
    result = await entry["fn"](parsed)
    return result.model_dump()
