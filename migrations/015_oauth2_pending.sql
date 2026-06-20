CREATE TABLE oauth2_pending (
    state TEXT PRIMARY KEY,
    data JSONB NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
