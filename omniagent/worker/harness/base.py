from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

EXECUTE_PYTHON_DESCRIPTION = (
    "Execute Python code in a sandboxed environment. "
    "The sandbox tools are available as plain functions (globals). "
    "Returns: JSON string of the LAST EXPRESSION in your code — NOT print() output. "
    "print() returns None and will give you null. Always end your code with a result variable as the last line."
)


def make_monty_executor(
    tool_snapshot: dict[str, Any],
    tool_executor: Callable[[str, dict], Awaitable[dict]],
    emit_event: Callable[[dict], Awaitable[None]],
) -> Callable[..., Awaitable[str]]:
    """Shared execute_python factory — emits tool_call/tool_result and runs monty."""
    from omniagent.worker.monty import make_monty_tool

    inner = make_monty_tool(tool_snapshot, tool_executor)

    async def execute_python(code: str, observation: str) -> str:
        await emit_event(
            {
                "type": "tool_call",
                "tool": "execute_python",
                "input": {"code": code, "observation": observation},
            }
        )
        try:
            result = await inner(code=code, observation=observation)
            await emit_event(
                {
                    "type": "tool_result",
                    "tool": "execute_python",
                    "success": True,
                    "input": {"code": code, "observation": observation},
                    "output": result,
                }
            )
            return result
        except Exception as exc:
            await emit_event(
                {
                    "type": "tool_result",
                    "tool": "execute_python",
                    "success": False,
                    "input": {"code": code, "observation": observation},
                    "error": str(exc),
                }
            )
            raise

    execute_python.__name__ = "execute_python"
    execute_python.__doc__ = EXECUTE_PYTHON_DESCRIPTION
    return execute_python


class HarnessAdapter(ABC):

    @abstractmethod
    async def run(
        self,
        system_prompt: str,
        history: list[dict],
        tool_executor: Callable[[str, dict], Awaitable[dict]],
        emit_event: Callable[[dict], Awaitable[None]],
        use_monty: bool,
        tool_snapshot: dict[str, Any],
        model: str = "",
    ) -> str:
        """Run agent loop. Returns final text response."""
        ...
