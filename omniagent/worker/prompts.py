"""System prompt construction and tool-name utilities."""

import json

from omniagent.worker.models import SessionConfig, ToolSnapshot
from omniagent.worker.native import NATIVE_TOOL_DESCRIPTIONS, NATIVE_TOOL_SCHEMAS


def _safe_tool_name(name: str) -> str:
    return name.replace(".", "__").replace("-", "_")


def _build_system_prompt(
    config: SessionConfig,
    extra_tools: dict[str, ToolSnapshot] | None = None,
) -> str:
    lines = [config.system_prompt]

    for toolbox in config.toolboxes:
        if toolbox.system_prompt:
            lines.append(toolbox.system_prompt)

    effective_snapshot = {**config.tool_snapshot, **(extra_tools or {})}

    if effective_snapshot and not config.use_monty:
        # Group tools by toolbox for clearer LLM context.
        by_toolbox: dict[str, list[tuple[str, ToolSnapshot]]] = {}
        for tool_name, schema in effective_snapshot.items():
            by_toolbox.setdefault(schema.skill_name or "", []).append((tool_name, schema))
        for skill_name, tools in by_toolbox.items():
            lines.append(f"\n## Toolbox: {skill_name}" if skill_name else "\nAvailable tools:")
            for tool_name, schema in tools:
                lines.append(f"- {tool_name}: {schema.description}")
                if schema.input_schema:
                    clean = {
                        **schema.input_schema,
                        "properties": {
                            k: {pk: pv for pk, pv in v.items() if pk != "x-param-in"}
                            for k, v in schema.input_schema.get("properties", {}).items()
                        },
                    }
                    lines.append(f"  Input schema: {json.dumps(clean)}")
                if schema.output_schema:
                    lines.append(f"  Output schema: {json.dumps(schema.output_schema)}")

    if effective_snapshot and config.use_monty:
        lines.append(
            "\nYou have access to one tool: `execute_python`. "
            "The sandbox exposes ONLY the functions listed below as globals — nothing else exists. "
            "There is NO internet, NO imports, NO urllib, NO requests, NO http, NO os, NO sys. "
            "Do NOT try to import anything. Do NOT probe the environment. Do NOT check available modules. "
            "JUST call the listed functions directly — they handle all networking and auth internally. "
            "ALWAYS write a SINGLE execute_python call that does everything — fetch all data, process it, and return the final answer. "
            "Never split work across multiple execute_python calls. "
            "Available functions (call these directly as globals):"
        )
        for tool_name, schema in effective_snapshot.items():
            fn = _safe_tool_name(tool_name)
            props = schema.input_schema.get("properties", {})
            params = ", ".join(f"{k}=..." for k in props)
            out_schema = schema.output_schema or {}
            out_props = out_schema.get("properties", out_schema)
            out_info = json.dumps(out_props) if out_props else "dict"
            lines.append(f"- {fn}({params})  -> {out_info}  # {schema.description}")
        lines.append(
            "\nIMPORTANT: Authentication (OAuth2, API keys, bearer tokens, basic auth) is handled AUTOMATICALLY "
            "by the framework. Do NOT attempt to fetch tokens or set headers manually. Just call the function directly.\n"
            "These functions return Python dicts DIRECTLY — there is NO 'result' wrapper key. "
            "Access fields directly, e.g.: `data = some_fn(x=1); value = data['field_name']`\n"
            "The LAST EXPRESSION in your code is the return value — it MUST be a variable or expression, NEVER a print() call. "
            "print() returns None and will discard all results. "
            "ALWAYS end your code with a bare variable name or expression: `result` not `print(result)`.\n"
            "ALWAYS include both `observation` (what you're doing) and `code` (the Python). "
            "Example: execute_python(observation='get Tokyo weather', code='result = get_weather(city=\"Tokyo\")\\nresult')"
        )

    return "\n".join(lines)


def _make_native_tool_snapshot(name: str) -> ToolSnapshot:
    return ToolSnapshot(
        name=name,
        description=NATIVE_TOOL_DESCRIPTIONS[name],
        input_schema=NATIVE_TOOL_SCHEMAS[name],
        output_schema={"type": "object"},
        skill_name="native",
        is_native=True,
    )
