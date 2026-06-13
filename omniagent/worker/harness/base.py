from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any


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
    ) -> str:
        """Run agent loop. Returns final text response."""
        ...
