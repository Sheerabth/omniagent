DROP TABLE IF EXISTS namespace_auth;
CREATE TABLE namespace_auth (
    namespace    TEXT NOT NULL,
    scheme_name  TEXT NOT NULL,
    auth_context BYTEA,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (namespace, scheme_name)
);
