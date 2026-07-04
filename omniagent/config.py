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

    # ── monty sandbox ────────────────────────────────────────────────────
    monty_executor_workers: int = 4
    monty_execution_timeout: int = 30

    # ── crypto ───────────────────────────────────────────────────────────
    omniagent_encryption_key: str = ""

    # ── tracing (langfuse — no-op if secret_key is unset) ────────────────
    langfuse_secret_key: str = ""
    langfuse_public_key: str = ""
    langfuse_host: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


settings = Settings()
