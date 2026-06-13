"""Claude Code harness adapter (via claude-agent-sdk + in-process MCP server)."""
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import query
from claude_agent_sdk.types import ClaudeAgentOptions, McpSdkServerConfig
from mcp.server import Server
from mcp.types import TextContent
from mcp.types import Tool as McpTool

from omniagent.worker.harness.base import HarnessAdapter

logger = logging.getLogger(__name__)


class ClaudeAdapter(HarnessAdapter):

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key

    async def run(
        self,
        system_prompt: str,
        history: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[dict]],
        emit_event: Callable[[dict], Awaitable[None]],
        use_monty: bool,
        tool_snapshot: dict[str, Any],
    ) -> str:
        mcp_server = _build_mcp_server(tool_snapshot, tool_executor, emit_event, use_monty)

        options = ClaudeAgentOptions(
            tools=[],
            system_prompt=system_prompt,
            mcp_servers={"omniagent": McpSdkServerConfig(
                type="sdk",
                name="omniagent",
                instance=mcp_server,
            )},
            permission_mode="bypassPermissions",
        )

        prompt = _build_prompt_with_history(history)
        await emit_event({"type": "thinking", "content": "Starting Claude agent"})

        final_text = ""
        async for message in query(prompt=prompt, options=options):
            if hasattr(message, "result"):
                final_text = message.result or final_text
            elif hasattr(message, "content"):
                content = message.content
                if isinstance(content, str):
                    final_text = content
                elif isinstance(content, list):
                    for block in content:
                        if getattr(block, "type", None) == "text":
                            final_text = block.text

        await emit_event({"type": "complete", "result": final_text})
        return final_text


def _build_mcp_server(
    tool_snapshot: dict[str, Any],
    tool_executor: Callable[[str, dict], Awaitable[dict]],
    emit_event: Callable[[dict], Awaitable[None]],
    use_monty: bool,
) -> Server:
    server = Server("omniagent-tools")

    tools = [
        McpTool(
            name=name,
            description=schema.get("description", ""),
            inputSchema=schema.get("input_schema", {"type": "object", "properties": {}}),
        )
        for name, schema in tool_snapshot.items()
    ]

    if use_monty:
        tools.append(McpTool(
            name="execute_python",
            description="Execute Python code in a pydantic-monty sandbox. Tools from the snapshot are available as functions.",
            inputSchema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python code to execute"},
                    "observation": {"type": "string", "description": "Why executing this code"},
                },
                "required": ["code", "observation"],
            },
        ))

    @server.list_tools()
    async def list_tools() -> list[McpTool]:
        return tools

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[TextContent]:
        if name == "execute_python" and use_monty:
            from omniagent.worker.monty import run_monty_code
            try:
                result = await run_monty_code(
                    code=arguments.get("code", ""),
                    tool_snapshot=tool_snapshot,
                    tool_executor=tool_executor,
                )
                return [TextContent(type="text", text=json.dumps(result))]
            except Exception as exc:
                return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

        await emit_event({"type": "tool_call", "tool": name, "input": arguments})
        try:
            output = await tool_executor(name, arguments)
            await emit_event({"type": "tool_result", "tool": name, "success": True})
            return [TextContent(type="text", text=json.dumps(output))]
        except Exception as exc:
            await emit_event({"type": "tool_result", "tool": name, "success": False})
            return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    return server


def _build_prompt_with_history(history: list[dict]) -> str:
    if not history:
        return ""
    prior = history[:-1]
    current = history[-1]
    if not prior:
        return current.get("content", "")
    lines = []
    for m in prior:
        role = m.get("role", "user").upper()
        lines.append(f"{role}: {m.get('content', '')}")
    lines.append(f"\nCurrent request:\n{current.get('content', '')}")
    return "\n".join(lines)
