from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

metadata = MetaData()

sessions = Table(
    "sessions",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("agent_name", Text, nullable=False, server_default=text("''")),
    Column("agent_version", Text, nullable=False, server_default=text("'v1'")),
    Column("toolbox_versions", JSONB, nullable=False, server_default=text("'{}'")),
    Column("tool_refs", ARRAY(Text), nullable=False, server_default=text("'{}'")),
    Column("status", Text, nullable=False, server_default=text("'idle'")),
    Column("messages", JSONB, nullable=False, server_default=text("'[]'")),
    Column("tool_calls", JSONB, nullable=False, server_default=text("'[]'")),
    Column("deferred_payload", Text, nullable=True),
    Column("schedule_id", UUID, nullable=True),
    Column("is_scheduled", Boolean, nullable=False, server_default=text("false")),
    Column("cancel_requested", Boolean, nullable=False, server_default=text("false")),
    Column("langfuse_trace_id", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
)

tools = Table(
    "tools",
    metadata,
    Column("name", Text, primary_key=True),
    Column("namespace", Text, nullable=False),
    Column("description", Text, nullable=False),
    Column("input_schema", JSONB, nullable=False, server_default=text("'{}'")),
    Column("output_schema", JSONB, nullable=False, server_default=text("'{}'")),
    Column("openapi_method", Text, nullable=False, server_default=text("''")),
    Column("openapi_path", Text, nullable=False, server_default=text("''")),
    Column("openapi_base_url", Text, nullable=False, server_default=text("''")),
    Column("openapi_security", JSONB, nullable=True),
    Column("timeout", Integer, nullable=True),
    Column("base_url", Text, nullable=False, server_default=text("''")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
)

toolboxes = Table(
    "toolboxes",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("name", Text, nullable=False),
    Column("version", Text, nullable=False, server_default=text("'v1'")),
    Column("tool_names", ARRAY(Text), nullable=False, server_default=text("'{}'")),
    Column("system_prompt", Text, nullable=False, server_default=text("''")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    UniqueConstraint("name", "version"),
)

agents = Table(
    "agents",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("name", Text, nullable=False),
    Column("version", Text, nullable=False, server_default=text("'v1'")),
    Column("harness", Text, nullable=False),
    Column("model", Text, nullable=False, server_default=text("''")),
    Column("toolbox_refs", JSONB, nullable=False, server_default=text("'{}'")),
    Column("tool_refs", ARRAY(Text), nullable=False, server_default=text("'{}'")),
    Column("system_prompt", Text, nullable=False, server_default=text("''")),
    Column("use_monty", Boolean, nullable=False, server_default=text("false")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    UniqueConstraint("name", "version"),
)

api_keys = Table(
    "api_keys",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("name", Text, unique=True, nullable=False),
    Column("key_hash", Text, nullable=False),
    Column("key_prefix", Text, nullable=False, server_default=text("''")),
    Column("scopes", ARRAY(Text), nullable=False, server_default=text("'{admin}'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
)

schedules = Table(
    "schedules",
    metadata,
    Column("id", UUID, primary_key=True, server_default=text("gen_random_uuid()")),
    Column("agent_name", Text, nullable=False),
    Column("cron_expr", Text, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("auth_context", Text, nullable=True),
    Column("enabled", Boolean, nullable=False, server_default=text("true")),
    Column("last_run_at", DateTime(timezone=True), nullable=True),
    Column("next_run_at", DateTime(timezone=True), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
)

agent_memory = Table(
    "agent_memory",
    metadata,
    Column("agent_name", Text, nullable=False),
    Column("key", Text, nullable=False),
    Column("value", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    PrimaryKeyConstraint("agent_name", "key"),
)

namespace_auth = Table(
    "namespace_auth",
    metadata,
    Column("namespace", Text, nullable=False),
    Column("scheme_name", Text, nullable=False),
    Column("auth_context", LargeBinary, nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=text("NOW()")),
    PrimaryKeyConstraint("namespace", "scheme_name"),
)

oauth_token_cache = Table(
    "oauth_token_cache",
    metadata,
    Column("cache_key", Text, primary_key=True),
    Column("token", Text, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)

oauth2_pending = Table(
    "oauth2_pending",
    metadata,
    Column("state", Text, primary_key=True),
    Column("data", JSONB, nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
)
