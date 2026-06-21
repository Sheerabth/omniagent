ALTER TABLE toolboxes DROP COLUMN instructions;
ALTER TABLE toolboxes DROP COLUMN toolbox_context;
ALTER TABLE toolboxes ADD COLUMN auth_context BYTEA;
ALTER TABLE agents DROP COLUMN auth_context;
ALTER TABLE schedules DROP COLUMN llm_context;
