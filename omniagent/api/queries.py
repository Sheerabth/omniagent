"""All SQL definitions used by API route files.

Simple CRUD uses SQLAlchemy Core expressions (select, insert, update, delete).
PostgreSQL-specific operations (jsonb ops, pg_notify, advisory locks, CAST,
ON CONFLICT, FOR UPDATE, IN with enum values) remain as text() wrappers.

Route/handler files MUST NOT define their own text() or Core expressions --
they import from here.
"""

from sqlalchemy import bindparam, delete, insert, select, text, update
from sqlalchemy.sql import func

from omniagent.tables import (
    agent_memory,
    agents,
    api_keys,
    namespace_auth,
    oauth2_pending,
    schedules,
    sessions,
    toolboxes,
    tools,
)

# ── PostgreSQL-specific text() wrappers ──────────────────────────────────────

update_session_messages_append = text(
    "UPDATE sessions SET messages = messages || CAST(:msg AS jsonb), status = :status, updated_at = NOW() WHERE id = :id"
)

upsert_agent_memory = text(
    "INSERT INTO agent_memory (agent_name, key, value, updated_at) "
    "VALUES (:agent_name, :key, CAST(:value AS jsonb), NOW()) "
    "ON CONFLICT (agent_name, key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
)

pg_notify = text("SELECT pg_notify(:channel, :payload)")

select_try_advisory_lock = text("SELECT pg_try_advisory_lock(hashtext('omniagent_reconcile'))")

select_advisory_unlock = text("SELECT pg_advisory_unlock(hashtext('omniagent_reconcile'))")

# ── Sessions ─────────────────────────────────────────────────────────────────

insert_session = insert(sessions).returning(
    sessions.c.id,
    sessions.c.agent_name,
    sessions.c.agent_version,
    sessions.c.toolbox_versions,
    sessions.c.tool_refs,
    sessions.c.status,
    sessions.c.created_at,
)

select_session_for_update = (
    select(sessions.c.status).where(sessions.c.id == bindparam("id")).with_for_update()
)

select_session_full = select(sessions).where(sessions.c.id == bindparam("id"))

select_sessions_recent = (
    select(sessions)
    .where(~sessions.c.is_scheduled)
    .order_by(sessions.c.created_at.desc())
    .limit(100)
)

# text() required: IN clause uses positional params from three SessionStatus
# enum values — SQLAlchemy Core's expanding bindparam can't handle the
# dynamic-length list cleanly with the same status IN for both the update's
# WHERE and another status elsewhere.
update_session_cancel_requested = text(
    "UPDATE sessions SET cancel_requested = true, updated_at = NOW() "
    "WHERE id = :id AND status IN (:s1, :s2, :s3)"
)

# text() required: dynamic IN clause pattern, and the "status = :where_status"
# check is a separate bound param from "status = :status".
update_session_status_pending_returning = text(
    "UPDATE sessions SET status = :status, messages = CAST(:messages AS jsonb), updated_at = NOW() "
    "WHERE id = :id AND status = :where_status RETURNING id"
)

delete_session_by_id = delete(sessions).where(sessions.c.id == bindparam("id"))

# ── Agents ───────────────────────────────────────────────────────────────────

select_agent_by_name_version = select(agents).where(
    agents.c.name == bindparam("name"),
    agents.c.version == bindparam("version"),
)

select_agent_latest = (
    select(agents)
    .where(agents.c.name == bindparam("name"))
    .order_by(agents.c.created_at.desc())
    .limit(1)
)

select_all_agents = select(agents).order_by(agents.c.name, agents.c.created_at)

select_agent_versions = (
    select(agents).where(agents.c.name == bindparam("name")).order_by(agents.c.created_at)
)

# text() required: CAST(:toolbox_refs AS jsonb), ON CONFLICT DO UPDATE
upsert_agent = text("""
    INSERT INTO agents (name, version, harness, model, toolbox_refs, tool_refs, system_prompt, use_monty)
    VALUES (:name, :version, :harness, :model, CAST(:toolbox_refs AS jsonb), :tool_refs, :system_prompt, :use_monty)
    ON CONFLICT (name, version) DO UPDATE SET
        harness       = EXCLUDED.harness,
        model         = EXCLUDED.model,
        toolbox_refs  = EXCLUDED.toolbox_refs,
        tool_refs     = EXCLUDED.tool_refs,
        system_prompt = EXCLUDED.system_prompt,
        use_monty     = EXCLUDED.use_monty,
        updated_at    = NOW()
    RETURNING *
""")

delete_agent_by_name_version = delete(agents).where(
    agents.c.name == bindparam("name"),
    agents.c.version == bindparam("version"),
)

select_tool_by_name = select(tools.c.name).where(tools.c.name == bindparam("name"))

select_toolbox_by_name_version = select(toolboxes.c.id).where(
    toolboxes.c.name == bindparam("name"),
    toolboxes.c.version == bindparam("version"),
)

# ── Tools ────────────────────────────────────────────────────────────────────

# text() required: ON CONFLICT DO UPDATE
upsert_tool = text("""
    INSERT INTO tools (name, namespace, description, input_schema, output_schema,
                       openapi_method, openapi_path, openapi_base_url, openapi_security, timeout)
    VALUES (:name, :namespace, :description, :input_schema, :output_schema,
            :openapi_method, :openapi_path, :openapi_base_url, :openapi_security, :timeout)
    ON CONFLICT (name) DO UPDATE SET
        namespace        = EXCLUDED.namespace,
        description      = EXCLUDED.description,
        input_schema     = EXCLUDED.input_schema,
        output_schema    = EXCLUDED.output_schema,
        openapi_method   = EXCLUDED.openapi_method,
        openapi_path     = EXCLUDED.openapi_path,
        openapi_base_url = EXCLUDED.openapi_base_url,
        openapi_security = EXCLUDED.openapi_security,
        timeout          = EXCLUDED.timeout,
        updated_at       = NOW()
""")

update_tool_timeout = (
    update(tools)
    .where(tools.c.name == bindparam("name"))
    .values(timeout=bindparam("timeout"), updated_at=func.now())
)

delete_tool_by_name = delete(tools).where(tools.c.name == bindparam("name"))

delete_tools_by_namespace = delete(tools).where(tools.c.namespace == bindparam("namespace"))

select_all_tools = select(tools).order_by(tools.c.name)

select_tools_by_namespace = (
    select(tools).where(tools.c.namespace == bindparam("namespace")).order_by(tools.c.name)
)

# ── Toolboxes ────────────────────────────────────────────────────────────────

# text() required: ON CONFLICT DO UPDATE
upsert_toolbox = text("""
    INSERT INTO toolboxes (name, version, tool_names, system_prompt)
    VALUES (:name, :version, :tool_names, :system_prompt)
    ON CONFLICT (name, version) DO UPDATE SET
        tool_names    = EXCLUDED.tool_names,
        system_prompt = EXCLUDED.system_prompt,
        updated_at    = NOW()
    RETURNING *
""")

select_all_toolboxes = select(toolboxes).order_by(toolboxes.c.name, toolboxes.c.created_at)

select_toolbox_versions = (
    select(toolboxes).where(toolboxes.c.name == bindparam("name")).order_by(toolboxes.c.created_at)
)

select_toolbox_by_name_and_version = select(toolboxes).where(
    toolboxes.c.name == bindparam("name"),
    toolboxes.c.version == bindparam("version"),
)

delete_toolbox_by_name_version = delete(toolboxes).where(
    toolboxes.c.name == bindparam("name"),
    toolboxes.c.version == bindparam("version"),
)

select_tool_names_by_name_list = select(tools.c.name).where(
    tools.c.name == bindparam("names", expanding=True)
)

# ── Schedules ────────────────────────────────────────────────────────────────

# text() required: jsonb INSERT with RETURNING specific columns
insert_schedule = text("""
    INSERT INTO schedules (agent_name, cron_expr, prompt, auth_context, enabled, next_run_at)
    VALUES (:agent_name, :cron_expr, :prompt, :auth_context, :enabled, :next_run_at)
    RETURNING id, agent_name, cron_expr, prompt, enabled, last_run_at, next_run_at, created_at, updated_at
""")

select_schedules = select(
    schedules.c.id,
    schedules.c.agent_name,
    schedules.c.cron_expr,
    schedules.c.prompt,
    schedules.c.enabled,
    schedules.c.last_run_at,
    schedules.c.next_run_at,
    schedules.c.created_at,
    schedules.c.updated_at,
).order_by(schedules.c.created_at.desc())

select_orphaned_scheduled_sessions = (
    select(sessions)
    .where(
        sessions.c.is_scheduled,
        sessions.c.schedule_id.is_(None),
    )
    .order_by(sessions.c.created_at.desc())
    .limit(200)
)

delete_sessions_by_schedule = delete(sessions).where(
    sessions.c.schedule_id == bindparam("schedule_id")
)

select_schedule_by_id = select(
    schedules.c.id,
    schedules.c.agent_name,
    schedules.c.cron_expr,
    schedules.c.prompt,
    schedules.c.enabled,
    schedules.c.last_run_at,
    schedules.c.next_run_at,
    schedules.c.created_at,
    schedules.c.updated_at,
).where(schedules.c.id == bindparam("id"))

select_sessions_by_schedule = (
    select(sessions)
    .where(sessions.c.schedule_id == bindparam("schedule_id"))
    .order_by(sessions.c.created_at.desc())
    .limit(50)
)

delete_schedule_by_id = delete(schedules).where(schedules.c.id == bindparam("id"))

# text() required: dynamic IN clause with SessionStatus enum values
cancel_sessions_for_schedule = text(
    "UPDATE sessions SET status = :status, updated_at = NOW() WHERE schedule_id = :schedule_id AND status = :where_status"
)


def build_update_schedule(sets: list[str]) -> str:
    """Build a dynamic UPDATE schedule query from a list of SET clauses."""
    cols = ", ".join(sets)
    return (
        f"UPDATE schedules SET {cols} WHERE id=:id "
        f"RETURNING id, agent_name, cron_expr, prompt, enabled, last_run_at, next_run_at, created_at, updated_at"
    )


# ── Memory ───────────────────────────────────────────────────────────────────

select_agent_memory = (
    select(agent_memory.c.key, agent_memory.c.value)
    .where(agent_memory.c.agent_name == bindparam("agent_name"))
    .order_by(agent_memory.c.key)
)

delete_agent_memory_key_value = delete(agent_memory).where(
    agent_memory.c.agent_name == bindparam("agent_name"),
    agent_memory.c.key == bindparam("key"),
)

delete_agent_memory_all = delete(agent_memory).where(
    agent_memory.c.agent_name == bindparam("agent_name"),
)

# ── Namespaces ───────────────────────────────────────────────────────────────

# text() required: COUNT(*) aggregate
select_namespaces_with_tool_count = text(
    "SELECT namespace, COUNT(*) AS tool_count FROM tools GROUP BY namespace ORDER BY namespace"
)

# text() required: ANY(:namespaces) array comparison
select_namespace_auth_by_namespace_list = text(
    "SELECT namespace, scheme_name, auth_context FROM namespace_auth WHERE namespace = ANY(:namespaces)"
)

# text() required: COUNT(*) aggregate
select_tool_count_by_namespace = text(
    "SELECT COUNT(*) AS c FROM tools WHERE namespace = :namespace"
)

# text() required: FOR UPDATE on a specific row
select_namespace_auth_for_update = text(
    "SELECT auth_context FROM namespace_auth WHERE namespace = :namespace AND scheme_name = :scheme_name FOR UPDATE"
)

# text() required: ON CONFLICT DO UPDATE
upsert_namespace_auth = text("""
    INSERT INTO namespace_auth (namespace, scheme_name, auth_context, updated_at)
    VALUES (:namespace, :scheme_name, :auth_context, NOW())
    ON CONFLICT (namespace, scheme_name) DO UPDATE
        SET auth_context = EXCLUDED.auth_context, updated_at = NOW()
""")

delete_namespace_auth = delete(namespace_auth).where(
    namespace_auth.c.namespace == bindparam("namespace"),
    namespace_auth.c.scheme_name == bindparam("scheme_name"),
)

# ── OAuth2 ───────────────────────────────────────────────────────────────────

select_tool_security = select(tools.c.openapi_security).where(
    tools.c.name == bindparam("tool_name")
)

# text() required: jsonb column access
select_namespace_auth_context = text(
    "SELECT auth_context FROM namespace_auth WHERE namespace = :namespace AND scheme_name = :scheme_name"
)

# text() required: NOW() < comparison
delete_expired_oauth_pending = text("DELETE FROM oauth2_pending WHERE expires_at < NOW()")

insert_oauth2_pending = insert(oauth2_pending).values(
    state=bindparam("state"),
    data=bindparam("data"),
    expires_at=bindparam("expires_at"),
)

# text() required: NOW() < comparison, RETURNING
delete_oauth2_pending_returning = text(
    "DELETE FROM oauth2_pending WHERE state = :state AND expires_at > NOW() RETURNING data"
)

# text() required: ON CONFLICT DO UPDATE
upsert_oauth2_namespace_auth = text("""
    INSERT INTO namespace_auth (namespace, scheme_name, auth_context, updated_at)
    VALUES (:namespace, :scheme_name, :auth_context, NOW())
    ON CONFLICT (namespace, scheme_name) DO UPDATE
        SET auth_context = EXCLUDED.auth_context, updated_at = NOW()
""")

# ── SSE ──────────────────────────────────────────────────────────────────────

select_session_status = select(sessions.c.status).where(sessions.c.id == bindparam("id"))

# ── Settings / API keys ──────────────────────────────────────────────────────

insert_api_key_returning = (
    insert(api_keys)
    .values(
        name=bindparam("name"),
        key_hash=bindparam("key_hash"),
        key_prefix=bindparam("key_prefix"),
        scopes=bindparam("scopes"),
    )
    .returning(api_keys.c.id, api_keys.c.name, api_keys.c.scopes, api_keys.c.created_at)
)

select_non_ui_api_keys = (
    select(api_keys.c.id, api_keys.c.name, api_keys.c.scopes, api_keys.c.created_at)
    .where(api_keys.c.name != "_built-in-ui")
    .order_by(api_keys.c.created_at)
)

delete_api_key_returning = (
    delete(api_keys)
    .where(
        api_keys.c.id == bindparam("id"),
        api_keys.c.name != "_built-in-ui",
    )
    .returning(api_keys.c.name)
)

select_api_key_by_id = select(api_keys.c.name).where(api_keys.c.id == bindparam("id"))

# ── Auth ─────────────────────────────────────────────────────────────────────

# text() required: key_prefix indexed lookup pattern
select_key_by_prefix = text("SELECT key_hash, scopes FROM api_keys WHERE key_prefix = :prefix")

# ── Main / lifecycle ─────────────────────────────────────────────────────────

# text() required: dynamic IN clause with SessionStatus
update_session_failed_where_running = text(
    "UPDATE sessions SET status=:status, updated_at=NOW() WHERE id=:id AND status=:where_status"
)

# text() required: dynamic IN clause with SessionStatus
select_stuck_sessions = text("SELECT id, status FROM sessions WHERE status IN (:s1, :s2)")
