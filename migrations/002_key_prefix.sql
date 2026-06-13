-- Add key_prefix for O(1) key lookup before argon2 verify
ALTER TABLE client_keys ADD COLUMN key_prefix TEXT NOT NULL DEFAULT '';
ALTER TABLE service_keys ADD COLUMN key_prefix TEXT NOT NULL DEFAULT '';

CREATE INDEX idx_client_keys_prefix ON client_keys(key_prefix);
CREATE INDEX idx_service_keys_prefix ON service_keys(key_prefix);
