from typing import Any

from pydantic import BaseModel, Field


class ToolBase(BaseModel):
    """LLM-visible base. observation is the only field here — always in schema."""

    observation: str = Field(default="", description="Why this tool is being called")


class ToolInput(ToolBase):
    """Adds worker-injected fields. Subclass this for tool inputs.
    Fields defined on ToolInput (not ToolBase) are stripped from LLM schema."""

    auth_context: Any = Field(
        default=None,
        description="Auth tokens/scopes — agent default, runtime override. Blind-piped, never seen by LLM.",
    )
    skill_context: Any = Field(
        default=None,
        description="Static skill-level config — set at skill definition, blind-piped, never seen by LLM.",
    )
    agent_name: str = Field(default="", description="Name of the agent that triggered this call.")
    skill_name: str = Field(default="", description="Name of the skill this tool belongs to.")

    @classmethod
    def model_json_schema(cls, **kwargs: Any) -> dict[str, Any]:
        schema = super().model_json_schema(**kwargs)
        injected = set(ToolInput.model_fields) - set(ToolBase.model_fields)
        props = {k: v for k, v in schema.get("properties", {}).items() if k not in injected}
        schema["properties"] = props
        schema["required"] = [k for k in schema.get("required", []) if k in props]
        if not schema["required"]:
            schema.pop("required", None)
        return schema


class ToolOutput(BaseModel):
    pass
