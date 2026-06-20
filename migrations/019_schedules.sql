-- Deferred turn payload (auth_context + llm_context for resumed turns)
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS deferred_payload TEXT;

-- Scheduled agent runs
CREATE TABLE IF NOT EXISTS schedules (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_name   TEXT        NOT NULL,
    cron_expr    TEXT        NOT NULL,
    prompt       TEXT        NOT NULL,
    llm_context  JSONB,
    auth_context TEXT,
    enabled      BOOLEAN     NOT NULL DEFAULT TRUE,
    last_run_at  TIMESTAMPTZ,
    next_run_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
