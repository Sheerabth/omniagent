from typing import Any

from pydantic import BaseModel, Field


class ToolInput(BaseModel):
    observation: str = Field(default="", description="Why this tool is being called")
    auth_context: Any = Field(
        default=None,
        description="Auth tokens/scopes — agent default, runtime override. Blind-piped, never seen by LLM.",
    )


class ToolOutput(BaseModel):
    pass
