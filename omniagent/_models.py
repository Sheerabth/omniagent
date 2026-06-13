from pydantic import BaseModel, Field


class ToolInput(BaseModel):
    observation: str = Field(default="", description="Why this tool is being called")


class ToolOutput(BaseModel):
    pass
