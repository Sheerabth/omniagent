"""SQL queries for the API control plane — separated from route/handler logic."""

from omniagent.constants import SessionStatus

# ── sessions ─────────────────────────────────────────────────────────────

select_session_for_update = "SELECT status FROM sessions WHERE id = %s FOR UPDATE"

select_session_status = "SELECT status FROM sessions WHERE id = %s"

select_session_full = "SELECT * FROM sessions WHERE id = %s"

insert_session = """
INSERT INTO sessions (agent_name, agent_version, toolbox_versions, tool_refs)
VALUES (%s, %s, %s, %s)
RETURNING *
"""

update_session_messages_append = """
UPDATE sessions
SET messages = messages || %s::jsonb, status = %s, updated_at = NOW()
WHERE id = %s
"""

update_session_status_returning = """
UPDATE sessions SET status=%s, messages=%s, updated_at=NOW()
WHERE id=%s AND status=%s RETURNING id
"""

select_sessions_recent = (
    "SELECT * FROM sessions WHERE is_scheduled = FALSE ORDER BY created_at DESC LIMIT 100"
)

update_session_cancel_requested = (
    "UPDATE sessions SET cancel_requested=true, updated_at=NOW() WHERE id=%s"
    " AND status IN (%s,%s,%s)"
)

delete_session_by_id = "DELETE FROM sessions WHERE id = %s"


def build_update_session_set_status(status: str) -> str:
    return f"UPDATE sessions SET status='{status}', updated_at=NOW() WHERE id=%s"


def build_update_session_set_status_where_status(status: str, where_status: str) -> str:
    return (
        f"UPDATE sessions SET status='{status}', updated_at=NOW()"
        f" WHERE id=%s AND status='{where_status}'"
    )


def build_update_session_status_pending_returning(status: str) -> str:
    return (
        f"UPDATE sessions SET status='{SessionStatus.PENDING}', messages=%s, updated_at=NOW()"
        f" WHERE id=%s AND status='{status}' RETURNING id"
    )


def build_cancel_running_sessions_by_schedule(schedule_status: str) -> str:
    return (
        f"UPDATE sessions SET status='{schedule_status}', updated_at=NOW()"
        f" WHERE schedule_id=%s AND status='{SessionStatus.PENDING}'"
    )


def build_select_stuck_sessions(*statuses: SessionStatus) -> str:
    quoted = ", ".join(f"'{s.value}'" for s in statuses)
    return f"SELECT id, status FROM sessions WHERE status IN ({quoted})"


# ── agents ──────────────────────────────────────────────────────────────

select_agent_by_name_version = "SELECT * FROM agents WHERE name = %s AND version = %s"

select_agent_latest = "SELECT * FROM agents WHERE name = %s ORDER BY created_at DESC LIMIT 1"

select_all_agents = "SELECT * FROM agents ORDER BY name, created_at"

select_agent_versions = "SELECT * FROM agents WHERE name = %s ORDER BY created_at"

upsert_agent = """
INSERT INTO agents (name, version, harness, model, toolbox_refs, tool_refs, system_prompt, use_monty)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (name, version) DO UPDATE
  SET harness       = EXCLUDED.harness,
      model         = EXCLUDED.model,
      toolbox_refs  = EXCLUDED.toolbox_refs,
      tool_refs     = EXCLUDED.tool_refs,
      system_prompt = EXCLUDED.system_prompt,
      use_monty     = EXCLUDED.use_monty,
      updated_at    = NOW()
RETURNING *
"""

delete_agent_by_name_version = "DELETE FROM agents WHERE name = %s AND version = %s"


# ── tools ───────────────────────────────────────────────────────────────

select_tool_by_name = "SELECT name FROM tools WHERE name = %s"

select_tool_security = "SELECT openapi_security FROM tools WHERE name=%s"

select_all_tools = "SELECT * FROM tools ORDER BY name"

select_tools_by_namespace = "SELECT * FROM tools WHERE namespace = %s ORDER BY name"

select_tool_count_by_namespace = "SELECT COUNT(*) AS c FROM tools WHERE namespace=%s"

upsert_tool = """
INSERT INTO tools
  (name, namespace, description, input_schema, output_schema,
   openapi_method, openapi_path, openapi_base_url, openapi_security, timeout)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (name) DO UPDATE
  SET namespace        = EXCLUDED.namespace,
      description      = EXCLUDED.description,
      input_schema     = EXCLUDED.input_schema,
      output_schema    = EXCLUDED.output_schema,
      openapi_method   = EXCLUDED.openapi_method,
      openapi_path     = EXCLUDED.openapi_path,
      openapi_base_url = EXCLUDED.openapi_base_url,
      openapi_security = EXCLUDED.openapi_security,
      timeout          = EXCLUDED.timeout,
      updated_at       = NOW()
"""

update_tool_timeout = "UPDATE tools SET timeout = %s, updated_at = NOW() WHERE name = %s"

delete_tool_by_name = "DELETE FROM tools WHERE name = %s"

delete_tools_by_namespace = "DELETE FROM tools WHERE namespace = %s"

select_namespaces_with_tool_count = (
    "SELECT namespace, COUNT(*) AS tool_count FROM tools GROUP BY namespace ORDER BY namespace"
)


# ── toolboxes ────────────────────────────────────────────────────────────

select_toolbox_by_name_version = "SELECT id FROM toolboxes WHERE name = %s AND version = %s"

select_tool_names_by_name_list = "SELECT name FROM tools WHERE name = ANY(%s)"

select_all_toolboxes = "SELECT * FROM toolboxes ORDER BY name, created_at"

select_toolbox_versions = "SELECT * FROM toolboxes WHERE name = %s ORDER BY created_at"

select_toolbox_by_name_and_version = "SELECT * FROM toolboxes WHERE name = %s AND version = %s"

upsert_toolbox = """
INSERT INTO toolboxes (name, version, tool_names, system_prompt)
VALUES (%s, %s, %s, %s)
ON CONFLICT (name, version) DO UPDATE
  SET tool_names    = EXCLUDED.tool_names,
      system_prompt = EXCLUDED.system_prompt,
      updated_at    = NOW()
RETURNING *
"""

delete_toolbox_by_name_version = "DELETE FROM toolboxes WHERE name = %s AND version = %s"


# ── agent_memory ─────────────────────────────────────────────────────────

select_agent_memory = "SELECT key, value FROM agent_memory WHERE agent_name=%s ORDER BY key"

upsert_agent_memory = """INSERT INTO agent_memory (agent_name, key, value, updated_at)
   VALUES (%s, %s, %s::jsonb, NOW())
   ON CONFLICT (agent_name, key) DO UPDATE SET value=EXCLUDED.value, updated_at=NOW()"""

delete_agent_memory_key_value = "DELETE FROM agent_memory WHERE agent_name=%s AND key=%s"

delete_agent_memory_all = "DELETE FROM agent_memory WHERE agent_name=%s"


# ── schedules ───────────────────────────────────────────────────────────

insert_schedule = """INSERT INTO schedules
   (agent_name, cron_expr, prompt, auth_context, enabled, next_run_at)
   VALUES (%s, %s, %s, %s, %s, %s)
   RETURNING id, agent_name, cron_expr, prompt, enabled,
             last_run_at, next_run_at, created_at, updated_at"""

select_schedules = (
    "SELECT id, agent_name, cron_expr, prompt, enabled, "
    "last_run_at, next_run_at, created_at, updated_at FROM schedules ORDER BY created_at DESC"
)

select_schedule_by_id = (
    "SELECT id, agent_name, cron_expr, prompt, enabled, "
    "last_run_at, next_run_at, created_at, updated_at FROM schedules WHERE id = %s"
)

update_schedule_returning = (
    "UPDATE schedules SET {sets} WHERE id=%s "
    "RETURNING id, agent_name, cron_expr, prompt, enabled, "
    "last_run_at, next_run_at, created_at, updated_at"
)

delete_schedule_by_id = "DELETE FROM schedules WHERE id = %s"

select_orphaned_scheduled_sessions = (
    "SELECT * FROM sessions WHERE is_scheduled = TRUE"
    " AND schedule_id IS NULL ORDER BY created_at DESC LIMIT 200"
)

select_sessions_by_schedule = (
    "SELECT * FROM sessions WHERE schedule_id = %s ORDER BY created_at DESC LIMIT 50"
)


# ── namespace_auth ────────────────────────────────────────────────────────

select_namespace_auth_by_namespace_list = (
    "SELECT namespace, scheme_name, auth_context FROM namespace_auth WHERE namespace = ANY(%s)"
)

select_namespace_auth_context = (
    "SELECT auth_context FROM namespace_auth WHERE namespace=%s AND scheme_name=%s"
)

select_namespace_auth_for_update = (
    "SELECT auth_context FROM namespace_auth WHERE namespace=%s AND scheme_name=%s FOR UPDATE"
)

upsert_namespace_auth = """INSERT INTO namespace_auth (namespace, scheme_name, auth_context, updated_at)
   VALUES (%s, %s, %s, NOW())
   ON CONFLICT (namespace, scheme_name) DO UPDATE
     SET auth_context = EXCLUDED.auth_context, updated_at = NOW()"""

delete_namespace_auth = "DELETE FROM namespace_auth WHERE namespace=%s AND scheme_name=%s"


# ── oauth2 ────────────────────────────────────────────────────────────────

delete_expired_oauth_pending = "DELETE FROM oauth2_pending WHERE expires_at < NOW()"

insert_oauth2_pending = "INSERT INTO oauth2_pending (state, data, expires_at) VALUES (%s, %s, %s)"

delete_oauth2_pending_returning = (
    "DELETE FROM oauth2_pending WHERE state=%s AND expires_at > NOW() RETURNING data"
)


# ── api_keys ─────────────────────────────────────────────────────────────

select_key_hash_and_scopes_by_prefix = "SELECT key_hash, scopes FROM api_keys WHERE key_prefix = %s"

insert_api_key_returning = (
    "INSERT INTO api_keys (name, key_hash, key_prefix, scopes)"
    " VALUES (%s, %s, %s, %s) RETURNING id, name, scopes, created_at"
)

select_non_ui_api_keys = (
    "SELECT id, name, scopes, created_at FROM api_keys"
    " WHERE name != '_built-in-ui' ORDER BY created_at"
)

delete_api_key_returning = (
    "DELETE FROM api_keys WHERE id = %s AND name != '_built-in-ui' RETURNING name"
)

select_api_key_by_id = "SELECT name FROM api_keys WHERE id = %s"


# ── pg_notify ────────────────────────────────────────────────────────────

pg_notify = "SELECT pg_notify(%s, %s)"


# ── misc ─────────────────────────────────────────────────────────────────

select_one = "SELECT 1"
select_try_advisory_lock = "SELECT pg_try_advisory_lock(hashtext('omniagent_reconcile'))"
select_advisory_unlock = "SELECT pg_advisory_unlock(hashtext('omniagent_reconcile'))"
