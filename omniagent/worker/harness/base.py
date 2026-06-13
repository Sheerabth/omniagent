from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

EXECUTE_PYTHON_DESCRIPTION = (
    "Execute Python code in a sandboxed environment. "
    "The sandbox tools are available as plain functions (globals). "
    "Returns: JSON string of the LAST EXPRESSION in your code — NOT print() output. "
    "print() returns None and will give you null. Always end your code with a result variable as the last line."
)


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
