CREATE TABLE oauth_token_cache (
    cache_key TEXT PRIMARY KEY,
    token TEXT NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL
);
