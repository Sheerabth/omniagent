"""SQL queries for the worker — separated from job/lifecycle/event logic.

Every ``await conn.execute(sql, ...)`` in the worker layer draws its SQL from
this module.  Static queries are plain ``str`` constants; dynamic queries
(parameterised by ``SessionStatus``, optional columns, etc.) use builder
functions that return the SQL string.

Naming conventions
------------------
- ``select_<table>_<what>``   — SELECT
- ``insert_<table>``          — INSERT
- ``update_<table>_<what>``   — UPDATE
- ``upsert_<table>``          — INSERT … ON CONFLICT DO UPDATE
- ``delete_<table>_<what>``   — DELETE
- ``<verb>_<table>_<what>_for_update`` — row-level lock
- ``build_<verb>_<table>_<what>``     — function returning SQL (dynamic parts)
"""

from omniagent.constants import SessionStatus

# ── sessions ─────────────────────────────────────────────────────────────

select_session_for_update = "SELECT status, messages FROM sessions WHERE id=%s"

select_session_langfuse_trace = "SELECT langfuse_trace_id FROM sessions WHERE id = %s"

update_session_langfuse_trace = "UPDATE sessions SET langfuse_trace_id = %s WHERE id = %s"

select_session_msg_count_and_cancel = (
    "SELECT jsonb_array_length(messages) as msg_count, cancel_requested "
    "FROM sessions WHERE id=%s FOR UPDATE"
)

select_session_messages = "SELECT messages FROM sessions WHERE id=%s"

update_session_set_status_running = (
    f"UPDATE sessions SET status='{SessionStatus.RUNNING}', updated_at=NOW() WHERE id=%s"
)

update_session_status_failed_running = f"UPDATE sessions SET status='{SessionStatus.FAILED}', updated_at=NOW() WHERE id=%s AND status='{SessionStatus.RUNNING}'"

update_session_tool_calls = "UPDATE sessions SET tool_calls = tool_calls || %s::jsonb WHERE id=%s"

# ── sessions (dynamic status-based queries) ──────────────────────────────


def build_update_session_status_where(from_status: str, to_status: str) -> str:
    """Build an UPDATE that transitions *from* a known status *to* another.

    The WHERE clause includes ``AND status='{from_status}'`` so that
    concurrent transitions that already moved past *from_status* are no-ops.
    """
    return (
        f"UPDATE sessions SET status='{to_status}', updated_at=NOW() "
        f"WHERE id=%s AND status='{from_status}'"
    )


def build_update_session_complete(clear_trace: str) -> str:
    """Build the UPDATE used by ``_complete_session`` after a successful turn.

    *clear_trace* is either ``''`` (when there is queued input — keep the
    Langfuse trace for the next turn) or ``', langfuse_trace_id = NULL'``
    (terminal state).
    """
    return (
        f"UPDATE sessions SET status=%s, "
        f"messages = messages || %s::jsonb, "
        f"updated_at=NOW(){clear_trace} WHERE id=%s"
    )


def build_update_session_insert_cancel_marker(clear_trace: str) -> str:
    """Build the UPDATE that inserts the ``[CANCELLED]`` marker into messages.

    Used by both ``_complete_session`` and ``_handle_defer`` when
    ``cancel_requested`` is true during the turn.
    """
    return (
        f"UPDATE sessions SET status=%s, "
        f"messages = jsonb_insert(messages, %s::text[], %s::jsonb), "
        f"cancel_requested=false, updated_at=NOW(){clear_trace} WHERE id=%s"
    )


def build_update_session_deferred_for_cancel() -> str:
    """Return: ``UPDATE sessions SET status='{DEFERRED}', … WHERE id=%s``.

    This is a trivial wrapper for consistency — the status is always
    ``deferred``.  Consumers may call this inline if they prefer.
    """
    return (
        f"UPDATE sessions SET status='{SessionStatus.DEFERRED}', "
        f"messages=%s, deferred_payload=%s, updated_at=NOW() WHERE id=%s"
    )


# ── sessions (config loading) ────────────────────────────────────────────

select_session_config = (
    "SELECT agent_name, agent_version, toolbox_versions, tool_refs " "FROM sessions WHERE id = %s"
)


# ── agents ──────────────────────────────────────────────────────────────

select_agent_by_name_version = "SELECT * FROM agents WHERE name = %s AND version = %s"

select_agent_latest_by_name = (
    "SELECT name, version, toolbox_refs, tool_refs FROM agents "
    "WHERE name = %s ORDER BY created_at DESC LIMIT 1"
)


# ── toolboxes ────────────────────────────────────────────────────────────

select_toolbox_by_name_version = "SELECT * FROM toolboxes WHERE name = %s AND version = %s"


# ── tools ────────────────────────────────────────────────────────────────

select_tools_by_names = "SELECT * FROM tools WHERE name = ANY(%s)"


# ── namespace_auth ───────────────────────────────────────────────────────

select_namespace_auth_by_namespaces = (
    "SELECT namespace, scheme_name, auth_context FROM namespace_auth " "WHERE namespace = ANY(%s)"
)


# ── agent_memory ─────────────────────────────────────────────────────────

select_memory_by_key = "SELECT value FROM agent_memory WHERE agent_name=%s AND key=%s"

upsert_agent_memory = """INSERT INTO agent_memory (agent_name, key, value, updated_at)
       VALUES (%s, %s, %s, NOW())
       ON CONFLICT (agent_name, key)
       DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"""

delete_agent_memory = "DELETE FROM agent_memory WHERE agent_name=%s AND key=%s"

select_memory_keys_by_agent = "SELECT key FROM agent_memory WHERE agent_name=%s ORDER BY key"


# ── schedules ────────────────────────────────────────────────────────────

select_schedules_due = (
    "SELECT id, agent_name, cron_expr, prompt FROM schedules "
    "WHERE enabled = TRUE AND (next_run_at IS NULL OR next_run_at <= NOW())"
)

update_schedule_fired = (
    "UPDATE schedules SET last_run_at=NOW(), next_run_at=%s, updated_at=NOW() WHERE id=%s"
)

select_schedule_active_session = (
    f"SELECT id FROM sessions WHERE schedule_id=%s AND "
    f"status IN ('{SessionStatus.PENDING}','{SessionStatus.RUNNING}') LIMIT 1"
)

insert_session_from_schedule = f"""INSERT INTO sessions (agent_name, agent_version, toolbox_versions, tool_refs, status, messages, schedule_id, is_scheduled)
       VALUES (%s, %s, %s, %s, '{SessionStatus.PENDING}', %s, %s, TRUE) RETURNING id"""

select_schedules_by_agent = "SELECT * FROM schedules WHERE agent_name=%s ORDER BY created_at DESC"

insert_schedule = """INSERT INTO schedules (agent_name, cron_expr, prompt, next_run_at)
       VALUES (%s, %s, %s, %s) RETURNING id"""

select_schedule_by_id_and_agent = (
    "SELECT cron_expr, prompt FROM schedules WHERE id=%s AND agent_name=%s"
)

update_schedule_by_id_and_agent = (
    "UPDATE schedules SET cron_expr=%s, prompt=%s, next_run_at=%s WHERE id=%s AND agent_name=%s"
)

cancel_sessions_for_schedule = (
    f"UPDATE sessions SET status='{SessionStatus.CANCELLED}', updated_at=NOW() "
    f"WHERE schedule_id=%s AND status='{SessionStatus.PENDING}'"
)

delete_schedule_by_id_and_agent = "DELETE FROM schedules WHERE id=%s AND agent_name=%s"


# ── oauth_token_cache ────────────────────────────────────────────────────

select_oauth_token_valid = (
    "SELECT token FROM oauth_token_cache WHERE cache_key=%s AND expires_at > NOW()"
)

delete_expired_oauth_tokens = "DELETE FROM oauth_token_cache WHERE expires_at < NOW()"

upsert_oauth_token = """INSERT INTO oauth_token_cache (cache_key, token, expires_at)
       VALUES (%s, %s, NOW() + %s * INTERVAL '1 second')
       ON CONFLICT (cache_key) DO UPDATE
       SET token=EXCLUDED.token, expires_at=EXCLUDED.expires_at"""


# ── pg_notify ────────────────────────────────────────────────────────────

pg_notify = "SELECT pg_notify(%s, %s)"
