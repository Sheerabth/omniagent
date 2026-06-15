"""omniagent SDK — library-style, framework-agnostic."""

import logging
import os
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

    Worker assertion validation is enabled automatically when
    OMNIAGENT_INTERNAL_KEY is set in the environment.
    """
    global _config

    ns = namespace or service
    _config = {
        "service": service,
        "namespace": ns,
        "control_plane": control_plane,
        "api_key": api_key,
        "internal_key": os.environ.get("OMNIAGENT_INTERNAL_KEY", ""),
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


async def handle_execute(
    tool: str,
    input: dict,
    context: Any = None,
    *,
    worker_assertion: str | None = None,
) -> dict:
    """Low-level — call after parsing the request yourself.

    Prefer handle_execute_from_request() for the common case.
    """
    return await _handle_execute_impl(
        tool=tool,
        input=input,
        context=context,
        worker_assertion=worker_assertion,
    )


async def handle_execute_from_request(body: dict, headers: dict[str, str]) -> dict:
    """Call from your /execute route — passes parsed body and headers.

    Extracts tool, input, context from body and X-OmniAgent-Assertion from headers.
    Validates worker assertion automatically when OMNIAGENT_INTERNAL_KEY is set.

    Raises ValueError (auth), KeyError (missing field / unknown tool). Map to
    your framework's error responses — all other exceptions are bubbled up
    as 500 equivalents.
    """
    if "tool" not in body:
        raise KeyError("Missing 'tool' in request body")
    if "input" not in body:
        raise KeyError("Missing 'input' in request body")
    return await _handle_execute_impl(
        tool=body["tool"],
        input=body["input"],
        context=body.get("context"),
        worker_assertion=headers.get("x-omniagent-assertion"),
    )


async def _handle_execute_impl(
    tool: str,
    input: dict,
    context: Any = None,
    *,
    worker_assertion: str | None = None,
) -> dict:
    """Core implementation — validates assertion, looks up tool, calls function."""
    internal_key: str = _config.get("internal_key", "")
    if internal_key:
        if not worker_assertion:
            raise ValueError("Missing X-OmniAgent-Assertion header")
        verify_worker_assertion(worker_assertion, internal_key)

    entry = _local_registry.get(tool)
    if entry is None:
        raise KeyError(f"Tool '{tool}' not found")
    merged = {**input}
    if context is not None:
        merged["context"] = context
    parsed = entry["input"].model_validate(merged)
    result = await entry["fn"](parsed)
    return result.model_dump()


def verify_worker_assertion(assertion: str, internal_key: str) -> dict:
    """Verify a JWT assertion from the OmniAgent worker.

    Returns the decoded claims dict on success. Raises ValueError on failure.

    Use in your /execute handler to confirm the request came from the worker:
        claims = verify_worker_assertion(header, os.environ["OMNIAGENT_INTERNAL_KEY"])
        assert claims["session_id"] == body.session_id
    """
    import jwt

    try:
        return jwt.decode(
            assertion,
            internal_key,
            algorithms=["HS256"],
            options={"require": ["exp", "iss", "session_id", "tool"]},
        )
    except jwt.InvalidTokenError as e:
        raise ValueError(f"Invalid worker assertion: {e}") from e
