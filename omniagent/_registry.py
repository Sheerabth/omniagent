from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class RegistryEntry(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    fn: Any
    input: Any
    output: Any
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    metadata: dict[str, Any] = {}


_local_registry: dict[str, RegistryEntry] = {}
