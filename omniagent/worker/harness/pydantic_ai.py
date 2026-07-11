"""Pydantic AI harness adapter — catchall for OpenAI, Groq, Mistral, DeepSeek, etc."""

import json
import logging
import os
from typing import Any

from pydantic_ai import Agent, Tool
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from omniagent.api.models import MessageRecord
from omniagent.config import settings
from omniagent.worker.harness._env import _load_env_file
from omniagent.worker.harness.base import HarnessAdapter, make_monty_executor
from omniagent.worker.models import EventEmitter, ThinkingEvent, ToolExecutor, ToolSnapshot

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "openai:gpt-4o"


class PydanticAIAdapter(HarnessAdapter):
    """Pydantic AI harness — multi-provider via a single adapter.

    Model string format: ``provider:model_id`` (e.g. ``openai:gpt-4o``,
    ``groq:llama-4-maverick``). Pydantic AI parses the prefix and selects
    the correct model class. API keys come from standard env vars
    (``OPENAI_API_KEY``, ``GROQ_API_KEY``, etc.), loaded from .env.pydantic.
    """

    def __init__(self, _lf_start_span: Any = None) -> None:
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
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> str:
        # Load .env.pydantic every run — edits take effect next job, no restart.
        os.environ.update(_load_env_file(settings.pydantic_env_file))

        tools: list[Tool[Any]] = _build_tools(tool_snapshot, tool_executor)

        if use_monty:
            monty = make_monty_executor(
                tool_snapshot, tool_executor, emit_event, _lf_start_span=self._lf_start_span
            )
            tools.append(
                Tool(
                    monty,
                    name="execute_python",
                    description="Execute Python code in a sandboxed environment",
                )
            )

        agent = Agent(
            model=model or _DEFAULT_MODEL,
            system_prompt=system_prompt,
            tools=tools,
        )

        message_history = _build_history(history, tool_calls or [])
        latest_user = next(
            (m.content for m in reversed(history) if m.role == "user"),
            "",
        )

        await emit_event(ThinkingEvent(content="Starting Pydantic AI agent"))

        result = await agent.run(latest_user, message_history=message_history)
        return result.output


def _make_tool_fn(
    tool_name: str,
    tool_executor: ToolExecutor,
) -> Any:
    async def fn(**kwargs: Any) -> str:
        try:
            output = await tool_executor(tool_name, dict(kwargs))
            return json.dumps(output)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    return fn


def _build_tools(
    tool_snapshot: dict[str, ToolSnapshot],
    tool_executor: ToolExecutor,
) -> list[Tool[Any]]:
    tools: list[Tool[Any]] = []
    for name, schema in tool_snapshot.items():
        tools.append(
            Tool.from_schema(
                _make_tool_fn(name, tool_executor),
                name=name,
                description=schema.description,
                json_schema=schema.input_schema,
            )
        )
    return tools


def _build_history(
    history: list[MessageRecord], tool_calls: list[dict[str, Any]]
) -> list[ModelRequest | ModelResponse]:
    messages: list[ModelRequest | ModelResponse] = []
    for m in history:
        if m.role == "user":
            messages.append(ModelRequest(parts=[UserPromptPart(content=m.content)]))
        elif m.role == "assistant":
            messages.append(ModelResponse(parts=[TextPart(content=m.content)]))

    # Weave previous tool calls/results into the history as structured messages
    # so the AI sees its prior tool interactions and doesn't re-trigger them.
    for tc in tool_calls:
        tool_name = tc.get("tool", "?")
        inp = tc.get("input", {})
        out = tc.get("output", tc.get("error", ""))
        messages.append(ModelResponse(parts=[ToolCallPart(tool_name=tool_name, args=inp)]))
        messages.append(ModelRequest(parts=[ToolReturnPart(tool_name=tool_name, content=out)]))

    return messages
