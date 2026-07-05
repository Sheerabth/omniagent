"""Antigravity (Gemini) harness adapter."""

import inspect
import json
import logging
import os
import subprocess
from contextvars import ContextVar
from typing import Any, Protocol

from google.antigravity import Agent

try:
    from google.antigravity.connections.local.local_connection_config import LocalAgentConfig
    from google.antigravity.hooks import policy
except ImportError as exc:
    raise ImportError(
        f"google-antigravity internal API mismatch: {exc}. "
        "Verify LocalAgentConfig and policy import paths for the installed package version."
    ) from exc

from omniagent.api.models import MessageRecord
from omniagent.config import settings
from omniagent.worker.harness._env import _load_env_file
from omniagent.worker.harness.base import HarnessAdapter, make_monty_executor
from omniagent.worker.models import (
    EventEmitter,
    MontyExecutor,
    ThinkingEvent,
    ToolExecutor,
    ToolSnapshot,
)

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "gemini-2.5-flash"

# google-antigravity's subprocess.Popen inherits os.environ — no upstream
# env override.  Replace with bare-minimum system vars listed in
# settings.antigravity_sandbox_env_vars.  Our worker settings are cached in
# the `settings` singleton at module load; stripping everything else is safe
# and keeps secrets out of the AI sandbox.
_SANDBOX_ENV: frozenset[str] | None = None


def _get_sandbox_env() -> frozenset[str]:
    """Lazily cache configured sandbox env var names."""
    global _SANDBOX_ENV
    if _SANDBOX_ENV is None:
        _SANDBOX_ENV = frozenset(settings.antigravity_sandbox_env_vars)
    assert _SANDBOX_ENV is not None  # narrow for pyright
    return _SANDBOX_ENV


_TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool}

# Per-job env for the Antigravity subprocess.  The patched Popen.__init__
# reads from this ContextVar — each concurrent job sets its own, so there's
# no race on os.environ or subprocess.Popen.__init__.
_antigravity_env_ctx: ContextVar[dict[str, str] | None] = ContextVar(
    "_antigravity_env_ctx", default=None
)


def _build_antigravity_env() -> dict[str, str]:
    """System baseline + .env.antigravity — secrets excluded."""
    env = {k: v for k, v in os.environ.items() if k in _get_sandbox_env()}
    env.update(_load_env_file(settings.antigravity_env_file))
    return env


# ── one-time monkey-patch at module load ────────────────────────────────

_original_popen_init = subprocess.Popen.__init__


def _patched_popen_init(self: Any, args: Any, **kwargs: Any) -> None:
    ctx_env = _antigravity_env_ctx.get()
    if ctx_env is not None and kwargs.get("env") is None:
        kwargs["env"] = ctx_env
    _original_popen_init(self, args, **kwargs)


subprocess.Popen.__init__ = _patched_popen_init  # pyright: ignore[reportAttributeAccessIssue]


def _assert_patch_intact() -> None:
    """Guard against another library clobbering our Popen.__init__ patch.

    ponytail: single global patch, no known conflicts in this venv today.
    If this ever fires, someone re-patched subprocess.Popen.__init__ without
    chaining to the previous value — fail loud so we don't silently leak the
    full process env (secrets included) into the AI sandbox subprocess.
    """
    if subprocess.Popen.__init__ is not _patched_popen_init:
        raise RuntimeError(
            "subprocess.Popen.__init__ was re-patched by another library — "
            "Antigravity env isolation is no longer in effect."
        )


class AntigravityTool(Protocol):
    """Dynamically-signed tool callable registered with google-antigravity."""

    async def __call__(self, **kwargs: Any) -> str: ...


class AntigravityAdapter(HarnessAdapter):

    def __init__(self, api_key: str | None = None, _lf_start_span: Any = None) -> None:
        self._api_key = api_key
        self._lf_start_span = _lf_start_span

    async def run(
        self,
        system_prompt: str,
        history: list[MessageRecord],
        tool_executor: ToolExecutor,
        emit_event: EventEmitter,
        use_monty: bool,
        tool_snapshot: dict[str, ToolSnapshot],
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
            model=model or _DEFAULT_MODEL,
        )

        latest_user = next(
            (m.content for m in reversed(history) if m.role == "user"),
            "",
        )

        await emit_event(ThinkingEvent(content="Starting Antigravity agent"))

        # Re-read .env.antigravity every run — edits take effect on the next
        # job, no restart needed. Merge (not restore) is safe here: no
        # await between this and Agent()'s sync validate/Popen calls below,
        # so no other job's task can interleave and observe a half-applied
        # write. Content is the same file for every job anyway — nothing to
        # isolate per-job, nothing to snapshot/restore.
        os.environ.update(_load_env_file(settings.antigravity_env_file))
        _assert_patch_intact()
        # Set this job's env on the ContextVar — the patched Popen.__init__
        # picks it up. No os.environ mutation, concurrent jobs don't race.
        _token = _antigravity_env_ctx.set(_build_antigravity_env())
        try:
            async with Agent(config) as agent:
                response = await agent.chat(latest_user)
                result = await _extract_text(response)
        finally:
            _antigravity_env_ctx.reset(_token)

        return result

    def _build_tool_callables(
        self,
        tool_snapshot: dict[str, ToolSnapshot],
        tool_executor: ToolExecutor,
        _emit_event: EventEmitter,
    ) -> list[AntigravityTool]:
        return [
            _make_tool_fn(name, schema, tool_executor) for name, schema in tool_snapshot.items()
        ]

    def _build_monty_tool(
        self,
        tool_snapshot: dict[str, ToolSnapshot],
        tool_executor: ToolExecutor,
        emit_event: EventEmitter,
    ) -> MontyExecutor:
        return make_monty_executor(
            tool_snapshot, tool_executor, emit_event, _lf_start_span=self._lf_start_span
        )


def _make_tool_fn(
    tool_name: str,
    schema: ToolSnapshot,
    tool_executor: ToolExecutor,
) -> AntigravityTool:
    props = schema.input_schema.get("properties", {})
    param_names = [k for k in props if k != "observation"]

    async def tool_fn(**kwargs: Any) -> str:
        input_data = dict(kwargs)
        try:
            output = await tool_executor(tool_name, input_data)
            return json.dumps(output)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    params = [
        inspect.Parameter("observation", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str)
    ]
    for p in param_names:
        ann = _TYPE_MAP.get(props[p].get("type", "string"), str)
        params.append(inspect.Parameter(p, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=ann))

    object.__setattr__(tool_fn, "__signature__", inspect.Signature(params))
    tool_fn.__name__ = _safe_name(tool_name)
    tool_fn.__doc__ = schema.description
    return tool_fn


def _safe_name(tool_name: str) -> str:
    return tool_name.replace(".", "__").replace("-", "_")


def _build_system_with_history(system_prompt: str, history: list[MessageRecord]) -> str:
    prior = [m for m in history[:-1] if m.role in ("user", "assistant")]
    if not prior:
        return system_prompt
    transcript = "\n".join(f"{m.role.upper()}: {m.content}" for m in prior)
    return f"{system_prompt}\n\n--- Prior conversation ---\n{transcript}\n--- End prior conversation ---"


async def _extract_text(response: Any) -> str:
    """Extract text content from an Antigravity response object.

    Handles known response shapes: .text() coroutine, .content str/list,
    and falls back to str() with a logged warning for unexpected types.
    """
    if hasattr(response, "text") and callable(response.text):
        return await response.text()  # pyright: ignore[reportGeneralTypeIssues]
    if hasattr(response, "content"):
        c = response.content
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            parts = []
            for part in c:
                if hasattr(part, "text"):
                    parts.append(part.text)
                else:
                    parts.append(str(part))
            return " ".join(parts)
    logger.warning(
        "_extract_text: unexpected response type %s, falling back to str()", type(response)
    )
    return str(response)
