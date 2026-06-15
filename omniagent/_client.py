"""omniagent SDK — library-style, framework-agnostic."""

import logging
import os
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel

from omniagent._registry import _local_registry

logger = logging.getLogger(__name__)


class ClientConfig(BaseModel):
    service: str = ""
    namespace: str = ""
    control_plane: str = ""
    api_key: str = ""
    internal_key: str = ""


_config: ClientConfig = ClientConfig()

BeforeHook = Callable[
    [str, dict[str, Any], Any, dict[str, Any]], Awaitable[None]
]  # tool, input, auth_context, metadata
AfterHook = Callable[
    [str, dict[str, Any], Any, dict[str, Any], dict[str, Any]], Awaitable[None]
]  # tool, input, auth_context, output, metadata
_before_hooks: list[BeforeHook] = []
_after_hooks: list[AfterHook] = []


def register_before_execute(hook: BeforeHook) -> None:
    """Register an async callback invoked before every tool execution.

    Signature: async def hook(tool: str, input: dict, auth_context: Any) -> None

    Raise an exception to block execution.  Hooks run in registration order.
    """
    _before_hooks.append(hook)


def register_after_execute(hook: AfterHook) -> None:
    """Register an async callback invoked after every tool execution.

    Signature: async def hook(tool: str, input: dict, auth_context: Any, output: dict) -> None

    Hooks always run — even if the tool function raised.  Exceptions from
    after-hooks are logged and swallowed (they cannot change the result).
    """
    _after_hooks.append(hook)


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
    _config = ClientConfig(
        service=service,
        namespace=ns,
        control_plane=control_plane,
        api_key=api_key,
        internal_key=os.environ.get("OMNIAGENT_INTERNAL_KEY", ""),
    )

    tools = [
        {
            "name": f"{ns}.{fn_name}",
            "description": entry.description,
            "input_schema": entry.input_schema,
            "output_schema": entry.output_schema,
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


async def handle_execute(body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    """Call from your /execute route — passes parsed body and headers.

    Extracts tool, input, auth_context, llm_context from body and
    X-OmniAgent-Assertion from headers.  Validates worker assertion
    automatically when OMNIAGENT_INTERNAL_KEY is set.

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
        auth_context=body.get("auth_context"),
        worker_assertion=headers.get("x-omniagent-assertion"),
    )


async def _handle_execute_impl(
    tool: str,
    input: dict[str, Any],
    auth_context: Any = None,
    *,
    worker_assertion: str | None = None,
) -> dict[str, Any]:
    """Core implementation — validates assertion, runs hooks, calls tool function."""
    internal_key: str = _config.internal_key
    if internal_key:
        if not worker_assertion:
            raise ValueError("Missing X-OmniAgent-Assertion header")
        verify_worker_assertion(worker_assertion, internal_key)

    entry = _local_registry.get(tool)
    if entry is None:
        raise KeyError(f"Tool '{tool}' not found")

    # Before-hooks — any exception blocks execution.
    for hook in _before_hooks:
        await hook(tool, input, auth_context, entry.metadata)
    merged = {**input}
    if auth_context is not None:
        merged["auth_context"] = auth_context
    parsed = entry.input.model_validate(merged)

    output: dict[str, Any] = {}
    try:
        result = await entry.fn(parsed)
        output = result.model_dump()
        return output
    except Exception:
        raise
    finally:
        # After-hooks always run.  Exceptions logged, never propagate.
        for hook in _after_hooks:
            try:
                await hook(tool, input, auth_context, output, entry.metadata)
            except Exception:
                logger.exception("after-execute hook failed for tool=%s", tool)


def verify_worker_assertion(assertion: str, internal_key: str) -> dict[str, Any]:
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
