CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE tools (
    name        TEXT PRIMARY KEY,
    namespace   TEXT NOT NULL,
    service     TEXT NOT NULL,
    description TEXT NOT NULL,
    input_schema  JSONB NOT NULL DEFAULT '{}',
    output_schema JSONB NOT NULL DEFAULT '{}',
    available   BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE skills (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT UNIQUE NOT NULL,
    tool_names   TEXT[] NOT NULL DEFAULT '{}',
    instructions TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE agents (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT UNIQUE NOT NULL,
    harness      TEXT NOT NULL,
    skill_names  TEXT[] NOT NULL DEFAULT '{}',
    system_prompt TEXT NOT NULL DEFAULT '',
    use_monty    BOOLEAN NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE sessions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id      UUID NOT NULL REFERENCES agents(id),
    status        TEXT NOT NULL DEFAULT 'active',
    tool_snapshot JSONB NOT NULL DEFAULT '{}',
    messages      JSONB NOT NULL DEFAULT '[]',
    tool_calls    JSONB NOT NULL DEFAULT '[]',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE client_keys (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    key_hash   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE service_keys (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name       TEXT NOT NULL,
    key_hash   TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- LLM API keys, encrypted AES-256-GCM
CREATE TABLE llm_keys (
    harness       TEXT PRIMARY KEY,
    encrypted_key BYTEA NOT NULL,
    key_hint      TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
