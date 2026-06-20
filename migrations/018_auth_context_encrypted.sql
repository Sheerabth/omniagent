ALTER TABLE agents ALTER COLUMN auth_context TYPE TEXT USING auth_context::TEXT;
