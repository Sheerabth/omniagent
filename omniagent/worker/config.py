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
    select_namespace_auth_by_namespaces,
    select_session_config,
    select_toolbox_by_name_version,
    select_tools_by_names,
)


async def _fetch_session_config(session_id: str) -> SessionConfig:
    async with get_conn() as conn:
        rows = await conn.execute(
            select_session_config,  # pyright: ignore[reportArgumentType]
            (session_id,),
        )
        session_row = await rows.fetchone()
        if not session_row:
            raise RuntimeError(f"session_not_found:{session_id}")
        session = _SessionConfigRow.model_validate(dict(session_row))

        rows = await conn.execute(
            select_agent_by_name_version,  # pyright: ignore[reportArgumentType]
            (session.agent_name, session.agent_version),
        )
        agent_row = await rows.fetchone()
        if not agent_row:
            raise RuntimeError(
                f"agent_version_deleted:{session.agent_name}:{session.agent_version}"
            )
        agent = AgentRecord.model_validate(dict(agent_row))

        # Load toolboxes
        toolboxes: list[ToolboxSnapshot] = []
        toolbox_tool_names: list[str] = []
        tool_to_toolbox: dict[str, str] = {}
        for tname, toolbox_version in session.toolbox_versions.items():
            rows = await conn.execute(
                select_toolbox_by_name_version,  # pyright: ignore[reportArgumentType]
                (tname, toolbox_version),
            )
            toolbox_row = await rows.fetchone()
            if not toolbox_row:
                raise RuntimeError(f"toolbox_version_deleted:{tname}:{toolbox_version}")
            toolbox = ToolboxRecord.model_validate(dict(toolbox_row))
            toolboxes.append(ToolboxSnapshot(system_prompt=toolbox.system_prompt or ""))
            for t in toolbox.tool_names:
                toolbox_tool_names.append(t)
                tool_to_toolbox[t] = tname

        # Batch-load all tools needed
        all_tool_names = list(set(toolbox_tool_names + session.tool_refs))
        tool_rows: dict[str, ToolRecord] = {}
        if all_tool_names:
            rows = await conn.execute(
                select_tools_by_names,  # pyright: ignore[reportArgumentType]
                (all_tool_names,),
            )
            for row in await rows.fetchall():
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
            rows = await conn.execute(
                select_namespace_auth_by_namespaces,  # pyright: ignore[reportArgumentType]
                (namespaces,),
            )
            ns_scheme_set = set(ns_scheme_pairs)
            for row in await rows.fetchall():
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
        toolboxes=toolboxes,
        tool_snapshot=tool_snapshot,
    )
