CREATE TABLE IF NOT EXISTS namespace_auth (
    namespace   TEXT PRIMARY KEY,
    auth_context BYTEA,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE toolboxes DROP COLUMN IF EXISTS auth_context;
