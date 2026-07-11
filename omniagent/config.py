"""Centralised settings — single source for all env vars and defaults.

Uses pydantic-settings (already a dependency). Reads from environment and
.env file automatically. Access via the module-level ``settings`` singleton.

    from omniagent.config import settings
    conn = await psycopg.AsyncConnection.connect(settings.database_url)
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ── required / core ──────────────────────────────────────────────────
    database_url: str = "postgresql://omniagent:omniagent@localhost:5432/omniagent"

    # ── control plane ────────────────────────────────────────────────────
    ui_password: str = ""
    ui_session_hours: int = 24
    log_level: str = "INFO"

    # ── worker ───────────────────────────────────────────────────────────
    worker_concurrency: int = 10
    tool_execution_timeout: int = 30
    antigravity_api_key: str = ""

    # ── monty sandbox ────────────────────────────────────────────────────
    monty_executor_workers: int = 4
    monty_execution_timeout: int = 30

    # ── worker queue ────────────────────────────────────────────────────────
    worker_queue_name: str = "default"

    # ── harness env files ───────────────────────────────────────────────────
    antigravity_env_file: str = ".env.antigravity"
    claude_env_file: str = ".env.claude"
    pydantic_env_file: str = ".env.pydantic"

    # ── antigravity sandbox ─────────────────────────────────────────────────
    antigravity_sandbox_env_vars: list[str] = [
        "PATH",
        "HOME",
        "PWD",
        "USER",
        "SHELL",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "PYTHON_VERSION",
        "PYTHON_SHA256",
    ]

    # ── auth / secrets cache ────────────────────────────────────────────────
    verify_cache_ttl_seconds: int = 60
    verify_cache_max_entries: int = 1000

    # ── auth tuning ─────────────────────────────────────────────────────────
    oauth_pending_expiry_minutes: int = 10
    token_expiry_buffer_seconds: int = 30

    # ── SSE hub ─────────────────────────────────────────────────────────────
    sse_poll_interval_seconds: float = 0.5
    sse_reconnect_max_backoff_seconds: int = 30

    # ── crypto ───────────────────────────────────────────────────────────
    omniagent_encryption_key: str = ""

    # ── tracing (langfuse — no-op if secret_key is unset) ────────────────
    langfuse_secret_key: str = ""
    langfuse_public_key: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
