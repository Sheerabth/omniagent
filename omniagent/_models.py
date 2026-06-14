from typing import Any

from pydantic import BaseModel, Field


class ToolInput(BaseModel):
    observation: str = Field(default="", description="Why this tool is being called")
    context: Any = Field(
        default=None, description="Opaque caller context forwarded from the session run request"
    )


class ToolOutput(BaseModel):
    pass
