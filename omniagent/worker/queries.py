"""All SQL queries and expressions used by the worker.

Every ``text()`` call and every Core expression that involves bind parameters
lives here — no worker file should contain inline SQL.  Worker files import
from here and only call ``conn.execute()``.

PostgreSQL-specific constructs (``jsonb`` ops, ``pg_notify``, ``CAST``) remain
as ``text()``; simple CRUD is written as SQLAlchemy Core expressions.
"""

from sqlalchemy import bindparam, func, insert, select, text, update

from omniagent.tables import (
    agent_memory,
    agents,
    namespace_auth,
    schedules,
    sessions,
    toolboxes,
    tools,
)

# ── Reusable value expressions ────────────────────────────────────────────────

now_expr = func.now()

# ── Session queries (job.py / lifecycle.py / config.py) ───────────────────────

# Lock session + fetch status + messages for turn execution
lock_session = (
    select(sessions.c.status, sessions.c.messages, sessions.c.tool_calls)
    .where(sessions.c.id == bindparam("session_id"))
    .with_for_update()
)

# Lock session + fetch message count (jsonb_array_length) + cancel flag
lock_session_with_msg_count = (
    select(
        text("jsonb_array_length(messages) as msg_count"),
        sessions.c.cancel_requested,
    )
    .where(sessions.c.id == bindparam("session_id"))
    .with_for_update()
)

# Fetch messages for a session
session_messages_by_id = select(sessions.c.messages).where(sessions.c.id == bindparam("session_id"))

# Fetch session config info
session_agent_by_id = select(
    sessions.c.agent_name,
    sessions.c.agent_version,
    sessions.c.toolbox_versions,
    sessions.c.tool_refs,
).where(sessions.c.id == bindparam("session_id"))

# Fetch langfuse trace id
session_langfuse_trace_id = select(sessions.c.langfuse_trace_id).where(
    sessions.c.id == bindparam("session_id")
)

# ── State transitions ─────────────────────────────────────────────────────────

# Update session status (used in job.py for pending→running)
set_session_status = (
    update(sessions)
    .where(sessions.c.id == bindparam("session_id"))
    .values(status=bindparam("_status"), updated_at=now_expr)
)

# Update langfuse trace id on session
update_session_langfuse_trace = (
    update(sessions)
    .where(sessions.c.id == bindparam("session_id"))
    .values(langfuse_trace_id=bindparam("_trace_id"))
)

# ── JSONB update operations (raw text, PostgreSQL-specific) ───────────────────

# Cancel path: jsonb_insert a marker at a specific position
update_session_cancel = text(
    "UPDATE sessions SET status = :status, "
    "messages = jsonb_insert(messages, CAST(:path AS text[]), CAST(:marker AS jsonb)), "
    "cancel_requested = false, updated_at = NOW() "
    "WHERE id = :session_id"
)

# Cancel path: same but also clear langfuse_trace_id
update_session_cancel_clear_trace = text(
    "UPDATE sessions SET status = :status, "
    "messages = jsonb_insert(messages, CAST(:path AS text[]), CAST(:marker AS jsonb)), "
    "cancel_requested = false, updated_at = NOW(), langfuse_trace_id = NULL "
    "WHERE id = :session_id"
)

# Normal complete path: append messages via jsonb ||
update_session_append_messages = text(
    "UPDATE sessions SET status = :status, "
    "messages = messages || CAST(:messages AS jsonb), "
    "updated_at = NOW() "
    "WHERE id = :session_id"
)

# Defer non-cancel path: replace messages + deferred_payload
update_session_deferred = text(
    "UPDATE sessions SET status = :status, "
    "messages = :messages, deferred_payload = :deferred_payload, "
    "updated_at = NOW() WHERE id = :session_id"
)

# ── pg_notify ─────────────────────────────────────────────────────────────────

select_pg_notify = text("SELECT pg_notify(:channel, :payload)")

# ── Event: set session to failed ──────────────────────────────────────────────

set_session_failed = (
    update(sessions)
    .where(
        sessions.c.id == bindparam("session_id"),
        sessions.c.status == bindparam("_status"),
    )
    .values(status=bindparam("_new_status"), updated_at=now_expr)
)

# ── Event: append tool call ───────────────────────────────────────────────────

update_session_tool_calls = text(
    "UPDATE sessions SET tool_calls = tool_calls || CAST(:tc AS jsonb), "
    "updated_at = NOW() WHERE id = :session_id"
)

# ── Agent / Toolbox / Tool queries (config.py) ────────────────────────────────

select_agent_by_name_version = select(agents).where(
    agents.c.name == bindparam("name"),
    agents.c.version == bindparam("version"),
)

select_toolbox_by_name_version = select(toolboxes).where(
    toolboxes.c.name == bindparam("name"),
    toolboxes.c.version == bindparam("version"),
)

select_tools_by_names = select(tools).where(tools.c.name.in_(bindparam("names", expanding=True)))

select_namespace_auth_by_namespace = select(
    namespace_auth.c.namespace,
    namespace_auth.c.scheme_name,
    namespace_auth.c.auth_context,
).where(namespace_auth.c.namespace == bindparam("namespaces"))

# ── Scheduler queries ─────────────────────────────────────────────────────────

select_agent_by_name_latest = (
    select(
        agents.c.name,
        agents.c.version,
        agents.c.toolbox_refs,
        agents.c.tool_refs,
    )
    .where(agents.c.name == bindparam("name"))
    .order_by(agents.c.created_at.desc())
    .limit(1)
)

select_active_session_by_schedule = (
    select(sessions.c.id)
    .where(
        sessions.c.schedule_id == bindparam("schedule_id"),
        sessions.c.status.in_(["pending", "running"]),
    )
    .limit(1)
)

insert_session_from_schedule = text(
    "INSERT INTO sessions "
    "(agent_name, agent_version, toolbox_versions, tool_refs, "
    "status, messages, schedule_id, is_scheduled) "
    "VALUES (:agent_name, :agent_version, :toolbox_versions, :tool_refs, "
    "'pending', :messages, :schedule_id, true) "
    "RETURNING id"
)

select_due_schedules = select(
    schedules.c.id,
    schedules.c.agent_name,
    schedules.c.cron_expr,
    schedules.c.prompt,
).where(
    schedules.c.enabled == True,  # noqa: E712
    text("(next_run_at IS NULL OR next_run_at <= NOW())"),
)

update_schedule_after_fire = (
    update(schedules)
    .where(schedules.c.id == bindparam("schedule_id"))
    .values(
        last_run_at=now_expr,
        next_run_at=bindparam("next_run_at"),
        updated_at=now_expr,
    )
)

# ── Memory queries (tools.py) ─────────────────────────────────────────────────

select_memory_value = select(agent_memory.c.value).where(
    agent_memory.c.agent_name == bindparam("agent_name"),
    agent_memory.c.key == bindparam("key"),
)

select_memory_keys = (
    select(agent_memory.c.key)
    .where(agent_memory.c.agent_name == bindparam("agent_name"))
    .order_by(agent_memory.c.key)
)

delete_memory = text("DELETE FROM agent_memory WHERE agent_name = :agent_name AND key = :key")

# ── Schedule list/update queries (tools.py) ───────────────────────────────────

select_schedules_by_agent = (
    select(schedules)
    .where(schedules.c.agent_name == bindparam("agent_name"))
    .order_by(schedules.c.created_at.desc())
)

select_schedule_by_id_agent = select(schedules.c.cron_expr, schedules.c.prompt).where(
    schedules.c.id == bindparam("schedule_id"),
    schedules.c.agent_name == bindparam("agent_name"),
)

update_schedule_by_id_agent = (
    update(schedules)
    .where(
        schedules.c.id == bindparam("schedule_id"),
        schedules.c.agent_name == bindparam("agent_name"),
    )
    .values(
        cron_expr=bindparam("cron_expr"),
        prompt=bindparam("prompt"),
        next_run_at=bindparam("next_run_at"),
    )
)

insert_schedule = (
    insert(schedules)
    .values(
        agent_name=bindparam("agent_name"),
        cron_expr=bindparam("cron_expr"),
        prompt=bindparam("prompt"),
        next_run_at=bindparam("next_run_at"),
    )
    .returning(schedules.c.id)
)

cancel_pending_sessions_by_schedule = text(
    "UPDATE sessions SET status = 'cancelled', updated_at = NOW() "
    "WHERE schedule_id = :schedule_id AND status = 'pending'"
)

delete_schedule = text("DELETE FROM schedules WHERE id = :schedule_id AND agent_name = :agent_name")

# ── Agent memory upsert (tools.py) ────────────────────────────────────────────

upsert_agent_memory = text(
    "INSERT INTO agent_memory (agent_name, key, value, updated_at) "
    "VALUES (:agent_name, :key, :value, NOW()) "
    "ON CONFLICT (agent_name, key) "
    "DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()"
)

# ── OAuth token cache queries (auth.py) ───────────────────────────────────────

select_oauth_token_valid = text(
    "SELECT token FROM oauth_token_cache WHERE cache_key = :cache_key AND expires_at > NOW()"
)

delete_expired_oauth_tokens = text("DELETE FROM oauth_token_cache WHERE expires_at < NOW()")

upsert_oauth_token = text(
    "INSERT INTO oauth_token_cache (cache_key, token, expires_at) "
    "VALUES (:cache_key, :token, NOW() + :ttl * INTERVAL '1 second') "
    "ON CONFLICT (cache_key) DO UPDATE "
    "SET token = EXCLUDED.token, expires_at = EXCLUDED.expires_at"
)
