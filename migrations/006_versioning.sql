-- Add version to skills
ALTER TABLE skills ADD COLUMN version TEXT NOT NULL DEFAULT 'v1';
ALTER TABLE skills DROP CONSTRAINT skills_name_key;
ALTER TABLE skills ADD CONSTRAINT skills_name_version_key UNIQUE (name, version);

-- Add version to agents and skill_refs column (keep skill_names until sessions migrated)
ALTER TABLE agents ADD COLUMN version TEXT NOT NULL DEFAULT 'v1';
ALTER TABLE agents DROP CONSTRAINT agents_name_key;
ALTER TABLE agents ADD CONSTRAINT agents_name_version_key UNIQUE (name, version);
ALTER TABLE agents ADD COLUMN skill_refs JSONB NOT NULL DEFAULT '{}';
UPDATE agents
SET skill_refs = COALESCE(
    (SELECT jsonb_object_agg(sn, 'v1') FROM unnest(skill_names) sn),
    '{}'::jsonb
);

-- Sessions: capture locked versions while skill_names still exists on agents
ALTER TABLE sessions ADD COLUMN agent_name TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN agent_version TEXT NOT NULL DEFAULT 'v1';
ALTER TABLE sessions ADD COLUMN skill_versions JSONB NOT NULL DEFAULT '{}';

UPDATE sessions s
SET agent_name    = a.name,
    agent_version = 'v1',
    skill_versions = COALESCE(
        (SELECT jsonb_object_agg(sn, 'v1') FROM unnest(a.skill_names) sn),
        '{}'::jsonb
    )
FROM agents a
WHERE s.agent_id = a.id;

-- Now drop old columns
ALTER TABLE agents DROP COLUMN skill_names;
ALTER TABLE sessions DROP CONSTRAINT sessions_agent_id_fkey;
ALTER TABLE sessions DROP COLUMN agent_id;
ALTER TABLE sessions DROP COLUMN tool_snapshot;
