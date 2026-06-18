ALTER TABLE tools DROP COLUMN IF EXISTS execute_url;
ALTER TABLE tools DROP COLUMN IF EXISTS service;

ALTER TABLE tools ADD COLUMN openapi_method   text NOT NULL DEFAULT '';
ALTER TABLE tools ADD COLUMN openapi_path     text NOT NULL DEFAULT '';
ALTER TABLE tools ADD COLUMN openapi_base_url text NOT NULL DEFAULT '';
ALTER TABLE tools ADD COLUMN openapi_security jsonb;
