"""Load session configuration from Postgres — agent, toolboxes, tools, auth."""

from omniagent.api.models import AgentRecord, ToolboxRecord, ToolRecord
from omniagent.crypto import decrypt_auth_context
from omniagent.db import get_conn
from omniagent.worker.models import (
    SessionConfig,
    ToolboxSnapshot,
    ToolSnapshot,
    _NamespaceAuthRow,
    _SessionConfigRow,
)
from omniagent.worker.queries import (
    select_agent_by_name_version,
    select_namespace_auth_by_namespace,
    select_toolbox_by_name_version,
    select_tools_by_names,
    session_agent_by_id,
)


async def _fetch_session_config(session_id: str) -> SessionConfig:
    async with get_conn() as conn:
        result = await conn.execute(
            session_agent_by_id,
            {"session_id": session_id},
        )
        session_row = result.mappings().fetchone()
        if not session_row:
            raise RuntimeError(f"session_not_found:{session_id}")
        session = _SessionConfigRow.model_validate(session_row)

        result = await conn.execute(
            select_agent_by_name_version,
            {"name": session.agent_name, "version": session.agent_version},
        )
        agent_row = result.mappings().fetchone()
        if not agent_row:
            raise RuntimeError(
                f"agent_version_deleted:{session.agent_name}:{session.agent_version}"
            )
        agent = AgentRecord.model_validate(agent_row)

        # Load toolboxes
        toolboxes_list: list[ToolboxSnapshot] = []
        toolbox_tool_names: list[str] = []
        tool_to_toolbox: dict[str, str] = {}
        for tname, toolbox_version in session.toolbox_versions.items():
            result = await conn.execute(
                select_toolbox_by_name_version,
                {"name": tname, "version": toolbox_version},
            )
            toolbox_row = result.mappings().fetchone()
            if not toolbox_row:
                raise RuntimeError(f"toolbox_version_deleted:{tname}:{toolbox_version}")
            toolbox = ToolboxRecord.model_validate(toolbox_row)
            toolboxes_list.append(ToolboxSnapshot(system_prompt=toolbox.system_prompt or ""))
            for t in toolbox.tool_names:
                toolbox_tool_names.append(t)
                tool_to_toolbox[t] = tname

        # Batch-load all tools needed
        all_tool_names = list(set(toolbox_tool_names + session.tool_refs))
        tool_rows: dict[str, ToolRecord] = {}
        if all_tool_names:
            result = await conn.execute(
                select_tools_by_names,
                {"names": all_tool_names},
            )
            for row in result.mappings().fetchall():
                tool = ToolRecord.model_validate(dict(row))
                tool_rows[tool.name] = tool

        # Batch-fetch auth by (namespace, scheme_name) pairs
        ns_scheme_pairs = list(
            {
                (t.namespace, (t.openapi_security or {}).get("scheme_name", ""))
                for t in tool_rows.values()
                if t.namespace and t.openapi_security
            }
        )
        ns_auth: dict[tuple[str, str], object] = {}
        if ns_scheme_pairs:
            namespaces = list({p[0] for p in ns_scheme_pairs})
            result = await conn.execute(
                select_namespace_auth_by_namespace,
                {"namespaces": namespaces},
            )
            ns_scheme_set = set(ns_scheme_pairs)
            for row in result.mappings().fetchall():
                r = _NamespaceAuthRow.model_validate(dict(row))
                pair = (r.namespace, r.scheme_name)
                if pair in ns_scheme_set and r.auth_context is not None:
                    ns_auth[pair] = decrypt_auth_context(r.auth_context)

        # Build tool_snapshot — toolbox tools first (take precedence over direct refs)
        tool_snapshot: dict[str, ToolSnapshot] = {}
        for name in [*toolbox_tool_names, *session.tool_refs]:
            if name in tool_rows and name not in tool_snapshot:
                t = tool_rows[name]
                tool_snapshot[name] = ToolSnapshot(
                    name=t.name,
                    description=t.description,
                    input_schema=t.input_schema,
                    output_schema=t.output_schema,
                    openapi_method=t.openapi_method,
                    openapi_path=t.openapi_path,
                    openapi_base_url=t.openapi_base_url,
                    openapi_security=t.openapi_security,
                    timeout=t.timeout,
                    skill_name=tool_to_toolbox.get(name, ""),
                    auth_context=ns_auth.get(
                        (t.namespace, (t.openapi_security or {}).get("scheme_name", ""))
                    ),
                )

    return SessionConfig(
        agent_name=session.agent_name,
        harness=agent.harness,
        model=agent.model,
        system_prompt=agent.system_prompt,
        use_monty=agent.use_monty,
        toolboxes=toolboxes_list,
        tool_snapshot=tool_snapshot,
    )
