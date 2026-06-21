"""Claude Code harness adapter (via claude-agent-sdk + in-process MCP server)."""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import query
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    McpSdkServerConfig,
    ResultMessage,
    TextBlock,
)
from mcp.server import Server
from mcp.types import TextContent
from mcp.types import Tool as McpTool

from omniagent.api.models import MessageRecord
from omniagent.worker.harness.base import (
    EXECUTE_PYTHON_DESCRIPTION,
    HarnessAdapter,
    make_monty_executor,
)
from omniagent.worker.models import BaseEvent, ThinkingEvent, ToolSnapshot

logger = logging.getLogger(__name__)


class ClaudeAdapter(HarnessAdapter):

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key

    async def run(
        self,
        system_prompt: str,
        history: list[MessageRecord],
        tool_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
        emit_event: Callable[[BaseEvent], Awaitable[None]],
        use_monty: bool,
        tool_snapshot: dict[str, ToolSnapshot],
        model: str = "",
    ) -> str:
        mcp_server = _build_mcp_server(tool_snapshot, tool_executor, emit_event, use_monty)

        options = ClaudeAgentOptions(
            tools=[],
            model=model or None,
            system_prompt=system_prompt,
            mcp_servers={
                "omniagent": McpSdkServerConfig(
                    type="sdk",
                    name="omniagent",
                    instance=mcp_server,
                )
            },
            permission_mode="bypassPermissions",
        )

        prompt = _build_prompt_with_history(history)
        await emit_event(ThinkingEvent(content="Starting Claude agent"))

        final_text = ""
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, ResultMessage):
                final_text = message.result or final_text
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        final_text = block.text

        return final_text


def _build_mcp_server(
    tool_snapshot: dict[str, ToolSnapshot],
    tool_executor: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    emit_event: Callable[[BaseEvent], Awaitable[None]],
    use_monty: bool,
) -> Server:
    server = Server("omniagent-tools")

    if use_monty:
        tools = []
    else:
        tools = [
            McpTool(
                name=name,
                description=schema.description,
                inputSchema=(
                    {
                        **schema.input_schema,
                        "properties": {
                            k: {pk: pv for pk, pv in v.items() if pk != "x-param-in"}
                            for k, v in schema.input_schema.get("properties", {}).items()
                        },
                    }
                    if schema.input_schema
                    else {"type": "object", "properties": {}}
                ),
            )
            for name, schema in tool_snapshot.items()
        ]

    if use_monty:
        tools.append(
            McpTool(
                name="execute_python",
                description=EXECUTE_PYTHON_DESCRIPTION,
                inputSchema={
                    "type": "object",
                    "properties": {
                        "code": {"type": "string", "description": "Python code to execute"},
                        "observation": {"type": "string", "description": "Why executing this code"},
                    },
                    "required": ["code", "observation"],
                },
            )
        )

    @server.list_tools()
    async def list_tools() -> list[McpTool]:
        return tools

    monty_handler = (
        make_monty_executor(tool_snapshot, tool_executor, emit_event) if use_monty else None
    )

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name == "execute_python":
            if not monty_handler:
                return [
                    TextContent(
                        type="text", text=json.dumps({"error": "monty not enabled on this agent"})
                    )
                ]
            try:
                result = await monty_handler(
                    code=arguments.get("code", ""),
                    observation=arguments.get("observation", ""),
                )
                return [TextContent(type="text", text=result)]
            except Exception as exc:
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

        try:
            output = await tool_executor(name, arguments)
            return [TextContent(type="text", text=json.dumps(output))]
        except Exception as exc:
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return server


def _build_prompt_with_history(history: list[MessageRecord]) -> str:
    if not history:
        return ""
    prior = history[:-1]
    current = history[-1]
    if not prior:
        return current.content
    lines = []
    for m in prior:
        lines.append(f"{m.role.upper()}: {m.content}")
    lines.append(f"\nCurrent request:\n{current.content}")
    return "\n".join(lines)
