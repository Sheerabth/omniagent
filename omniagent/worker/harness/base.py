from abc import ABC, abstractmethod
from typing import Any

from omniagent.api.models import FileRef, MessageRecord
from omniagent.storage import StorageClient
from omniagent.worker.models import (
    EventEmitter,
    MontyExecutor,
    ToolCallEvent,
    ToolExecutor,
    ToolResultEvent,
    ToolSnapshot,
)

EXECUTE_PYTHON_DESCRIPTION = (
    "Execute Python code in a sandboxed environment. "
    "The sandbox tools are available as plain functions (globals). "
    "Returns: JSON string of the LAST EXPRESSION in your code — NOT print() output. "
    "print() returns None and will give you null. Always end your code with a result variable as the last line."
)


def make_monty_executor(
    tool_snapshot: dict[str, ToolSnapshot],
    tool_executor: ToolExecutor,
    emit_event: EventEmitter,
    _lf_start_span: Any = None,
) -> MontyExecutor:
    """Shared execute_python factory — emits tool_call/tool_result and runs monty.

    If *_lf_start_span* is given, it is called as
    ``_lf_start_span(name, input) -> span | None`` before execution and the
    returned span is updated/ended afterward — this puts monty code into
    the Langfuse trace like any other tool call.
    """
    from omniagent.worker.monty import make_monty_tool

    inner = make_monty_tool(tool_snapshot, tool_executor)

    async def execute_python(code: str, observation: str) -> str:
        await emit_event(
            ToolCallEvent(tool="execute_python", input={"code": code, "observation": observation})
        )
        _lf_span = (
            _lf_start_span("execute_python", {"code": code, "observation": observation})
            if _lf_start_span
            else None
        )
        try:
            result = await inner(code=code, observation=observation)
            await emit_event(
                ToolResultEvent(
                    tool="execute_python",
                    success=True,
                    input={"code": code, "observation": observation},
                    output=result,
                )
            )
            if _lf_span:
                _lf_span.update(output=result)
                _lf_span.end()
            return result
        except Exception as exc:
            await emit_event(
                ToolResultEvent(
                    tool="execute_python",
                    success=False,
                    input={"code": code, "observation": observation},
                    error=str(exc),
                )
            )
            if _lf_span:
                _lf_span.update(output=str(exc))
                _lf_span.end()
            raise

    execute_python.__name__ = "execute_python"
    execute_python.__doc__ = EXECUTE_PYTHON_DESCRIPTION
    return execute_python


def embed_files(files: list[FileRef]) -> str:
    """List attached files — model uses native.file_read to inspect content."""
    parts = [
        "[Files attached to this message. Call file_read(path='...') to get file content. "
        "Files are in remote storage, not the sandbox filesystem — don't try open() or import. "
        "Use file_list(prefix='...') to browse, file_write(path, content) / file_append to create.]"
    ]
    for ref in files:
        parts.append(f"- {ref.name} ({ref.content_type}, {ref.size} bytes) -> path='{ref.path}'")
    return "\n".join(parts)


class HarnessAdapter(ABC):

    @abstractmethod
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
        files: list[FileRef] | None = None,
        session_id: str = "",
        storage: StorageClient | None = None,
    ) -> str:
        """Run agent loop. Returns final text response.

        *files* are FileRefs attached to the current user turn.
        Text documents are inspected via native.file_read at runtime.
        Media files (image/audio/video) are passed as content blocks
        when *session_id* and *storage* are provided.
        """
        ...
