import asyncio
import functools
import typing
from collections.abc import Callable
from typing import Any, TypeVar

from omniagent._models import ToolInput, ToolOutput
from omniagent._registry import _local_registry

F = TypeVar("F", bound=Callable[..., Any])


def tool(description: str | None = None, **metadata: Any) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        try:
            hints = typing.get_type_hints(fn)
        except NameError as e:
            raise TypeError(
                f"@tool function '{fn.__name__}': cannot resolve type hints ({e}). "
                f"Add 'from __future__ import annotations' or use fully qualified types."
            ) from e
        params = list(hints.items())

        input_type = next((t for k, t in params if k != "return"), None)
        output_type = hints.get("return")

        if input_type is None or not issubclass(input_type, ToolInput):
            raise TypeError(
                f"@tool function '{fn.__name__}': first parameter must subclass ToolInput"
            )
        if output_type is None or not issubclass(output_type, ToolOutput):
            raise TypeError(f"@tool function '{fn.__name__}': return type must subclass ToolOutput")

        desc = description or (fn.__doc__ or "").strip()
        if not desc:
            raise TypeError(
                f"@tool function '{fn.__name__}': description required (decorator arg or docstring)"
            )

        if asyncio.iscoroutinefunction(fn):
            wrapped = fn
        else:

            @functools.wraps(fn)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                return await asyncio.get_event_loop().run_in_executor(
                    None, functools.partial(fn, *args, **kwargs)
                )

        from omniagent._registry import RegistryEntry

        _local_registry[fn.__name__] = RegistryEntry(
            fn=wrapped,
            input=input_type,
            output=output_type,
            description=desc,
            input_schema=input_type.model_json_schema(),
            output_schema=output_type.model_json_schema(),
            metadata=metadata,
        )

        # Return original fn (preserves calling convention); async wrapper
        # is stored in _local_registry for the worker to invoke.
        return fn  # type: ignore[return-value]

    return decorator
