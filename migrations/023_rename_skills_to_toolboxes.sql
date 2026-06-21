ALTER TABLE skills RENAME TO toolboxes;
ALTER TABLE toolboxes RENAME COLUMN skill_context TO toolbox_context;
ALTER TABLE agents RENAME COLUMN skill_refs TO toolbox_refs;
ALTER TABLE sessions RENAME COLUMN skill_versions TO toolbox_versions;
ALTER TABLE agents ADD COLUMN IF NOT EXISTS tool_refs TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS tool_refs TEXT[] NOT NULL DEFAULT '{}';
