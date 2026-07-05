"""Procrastinate worker task: run_agent_job.

This is the orchestrator — session validation, config loading, langfuse
tracing, harness dispatch. Tool execution, event emission, session lifecycle,
auth, and prompt construction live in their own modules under ``worker/``.
"""

import contextlib
import logging
import time
from typing import Any, Protocol

import procrastinate
from langfuse import Langfuse, propagate_attributes
from procrastinate import PsycopgConnector

from omniagent.api.models import MessageRecord
from omniagent.config import settings
from omniagent.db import get_conn
from omniagent.logging_config import trace_id_var
from omniagent.worker.config import _fetch_session_config
from omniagent.worker.events import _emit_event
from omniagent.worker.lifecycle import _complete_session, _handle_defer
from omniagent.worker.models import BaseEvent, ErrorEvent, SystemPromptEvent
from omniagent.worker.native import NATIVE_TOOL_DESCRIPTIONS, DeferInfo
from omniagent.worker.prompts import _build_system_prompt, _make_native_tool_snapshot
from omniagent.worker.tools import NativeToolContext, NativeToolExecutor

logger = logging.getLogger(__name__)


class _LangfuseOp(Protocol):
    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


def _safe_lf(
    fn: _LangfuseOp, *args: Any, _warning: str = "langfuse call failed", **kwargs: Any
) -> Any:
    """Call *fn* and return its result, or ``None`` on failure. Never raises.

    Every langfuse call goes through this — tracing must never block AI flows.
    """
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("%s: %s", _warning, exc)
        return None


# ponytail: no-op if LANGFUSE_SECRET_KEY not set — no config change needed
# for deployments that don't run langfuse.
_langfuse = Langfuse() if settings.langfuse_secret_key else None

app = procrastinate.App(connector=PsycopgConnector(conninfo=settings.database_url))


@app.task(name="run_agent_job", queue="default")
async def run_agent_job(session_id: str) -> None:
    trace_id_var.set(session_id)
    job_start = time.monotonic()

    async with get_conn() as conn:
        row = await (
            await conn.execute("SELECT status, messages FROM sessions WHERE id=%s", (session_id,))
        ).fetchone()
        if not row:
            logger.warning("run_agent_job: session %s not found, skipping", session_id)
            return
        if row["status"] == "cancelled":
            logger.info("run_agent_job: session %s cancelled, skipping", session_id)
            return
        history = [MessageRecord.model_validate(m) for m in (row["messages"] or [])]
        if row["status"] in ("pending", "deferred"):
            await conn.execute(
                "UPDATE sessions SET status='running', updated_at=NOW() WHERE id=%s",
                (session_id,),
            )
            await _emit_event(session_id, BaseEvent(type="running"))

    config = await _fetch_session_config(session_id)
    harness = config.harness
    model = config.model
    use_monty = config.use_monty

    # Inject native tools — must happen before building system prompt
    native_tools = {name: _make_native_tool_snapshot(name) for name in NATIVE_TOOL_DESCRIPTIONS}
    tool_snapshot = {**config.tool_snapshot, **native_tools}
    system_prompt = _build_system_prompt(config, extra_tools=native_tools)

    # Shared defer state — set by native.defer_turn / native.defer_turn_until
    _defer_state: dict[str, DeferInfo] = {}

    # Langfuse trace — best-effort, must never block the AI flow.
    # Last real user message (skip [RESUME] and [CANCELLED] markers).
    last_user = next(
        (
            m.content
            for m in reversed(history)
            if m.role == "user" and not m.content.startswith("[")
        ),
        None,
    )
    # Closures below capture `trace` by name — it's None until the
    # start_as_current_observation context manager assigns it.
    trace = None

    # ── Tool execution ───────────────────────────────────────────────────
    async def emit(event: BaseEvent) -> None:
        await _emit_event(session_id, event)

    native_ctx = NativeToolContext(
        session_id=session_id,
        agent_name=config.agent_name,
        harness=harness,
        tool_snapshot=tool_snapshot,
        defer_state=_defer_state,
    )
    native_exec = NativeToolExecutor(native_ctx, emit)

    async def tool_exec(tool_name: str, input_data: dict[str, Any]) -> dict[str, Any]:
        _lf_span = (
            _safe_lf(
                trace.start_observation,
                name=tool_name,
                as_type="span",
                input=input_data,
                _warning="langfuse tool span failed",
            )
            if trace
            else None
        )
        result = await native_exec.execute(tool_name, input_data)
        if _lf_span:
            _safe_lf(_lf_span.update, output=result, _warning="langfuse span end failed")
            _safe_lf(_lf_span.end, _warning="langfuse span end failed")
        return result

    # ── Langfuse trace setup ─────────────────────────────────────────────
    # One Langfuse trace per session — chained turns (defer → follow-up)
    # reuse the same trace_id so they appear as one trace in the UI.
    _lf_existing_trace_id: str | None = None
    if _langfuse:
        try:
            async with get_conn() as conn:
                rows = await conn.execute(
                    "SELECT langfuse_trace_id FROM sessions WHERE id = %s",
                    (session_id,),
                )
                row = await rows.fetchone()
            if row and row["langfuse_trace_id"]:
                _lf_existing_trace_id = row["langfuse_trace_id"]
        except Exception:
            pass

    _lf_root_ctx = contextlib.nullcontext()
    if _langfuse:
        _lf_kwargs: dict[str, Any] = {
            "name": config.agent_name,
            "as_type": "span",
            "input": last_user,
            "metadata": {"harness": harness, "model": model, "monty": use_monty},
        }
        if _lf_existing_trace_id:
            # Follow-up job — join existing trace.
            _lf_kwargs["trace_context"] = {"trace_id": _lf_existing_trace_id}
        # For new traces, don't pass trace_context — let
        # start_as_current_observation create the trace naturally.
        _lf_root_ctx = (
            _safe_lf(
                _langfuse.start_as_current_observation,
                _warning="langfuse trace creation failed",
                **_lf_kwargs,
            )
            or contextlib.nullcontext()
        )

    with _lf_root_ctx as root_span:
        if root_span is not None:
            trace = root_span  # closures see the real trace via name capture
            # Persist trace_id from the actual trace (first job only).
            if _langfuse and not _lf_existing_trace_id:
                try:
                    _actual_id = _langfuse.get_current_trace_id()
                    if _actual_id:
                        async with get_conn() as conn:
                            await conn.execute(
                                "UPDATE sessions SET langfuse_trace_id = %s WHERE id = %s",
                                (_actual_id, session_id),
                            )
                except Exception:
                    pass

        prop_ctx = contextlib.nullcontext()
        if _langfuse:
            prop_ctx = (
                _safe_lf(
                    propagate_attributes,
                    session_id=session_id,
                    user_id=config.agent_name,
                    _warning="langfuse propagate_attributes failed",
                )
                or contextlib.nullcontext()
            )

        with prop_ctx:
            await emit(SystemPromptEvent(content=system_prompt, input=history))

            try:
                # Langfuse span factory for monty code — thread through
                # the adapter so execute_python calls appear in trace.
                def _mk_lf_span(name: str, input_data: Any) -> Any:
                    if not trace:
                        return None
                    return _safe_lf(
                        trace.start_observation,
                        name=name,
                        as_type="span",
                        input=input_data,
                        _warning=f"langfuse {name} span failed",
                    )

                if harness == "antigravity":
                    from omniagent.worker.harness.antigravity import AntigravityAdapter

                    adapter = AntigravityAdapter(
                        api_key=settings.antigravity_api_key or None,
                        _lf_start_span=_mk_lf_span,
                    )
                elif harness == "claude":
                    from omniagent.worker.harness.claude import ClaudeAdapter

                    adapter = ClaudeAdapter(_lf_start_span=_mk_lf_span)
                else:
                    raise ValueError(f"Unknown harness: {harness!r}")

                generation = (
                    _safe_lf(
                        trace.start_observation,
                        name=f"{harness}/{model}",
                        as_type="generation",
                        model=model,
                        input=last_user,
                        _warning="langfuse generation creation failed",
                    )
                    if trace
                    else None
                )
                result = await adapter.run(
                    system_prompt=system_prompt,
                    history=history,
                    tool_executor=tool_exec,
                    emit_event=emit,
                    use_monty=use_monty,
                    tool_snapshot=tool_snapshot,
                    model=model,
                )
                if generation:
                    _safe_lf(
                        generation.update,
                        output=result,
                        _warning="langfuse generation end failed",
                    )
                    _safe_lf(generation.end, _warning="langfuse generation end failed")
                if trace:
                    # trace is start_as_current_observation — the context
                    # manager's __exit__ calls end() automatically.  Only update.
                    _safe_lf(trace.update, output=result, _warning="langfuse trace update failed")
                if _langfuse:
                    _safe_lf(_langfuse.flush, _warning="langfuse flush failed")

                if defer := _defer_state.get("info"):
                    await _handle_defer(session_id, result, history, defer)
                    outcome = "deferred"
                else:
                    await _complete_session(session_id, result, len(history))
                    outcome = "completed"
                logger.info(
                    "run_agent_job finished",
                    extra={
                        "session_id": session_id,
                        "outcome": outcome,
                        "duration_ms": round((time.monotonic() - job_start) * 1000),
                    },
                )

            except Exception as exc:
                logger.exception(
                    "run_agent_job failed for session %s",
                    session_id,
                    extra={"duration_ms": round((time.monotonic() - job_start) * 1000)},
                )
                await emit(ErrorEvent(reason=str(exc)))
                raise
