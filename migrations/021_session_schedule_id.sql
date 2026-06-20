ALTER TABLE sessions ADD COLUMN IF NOT EXISTS schedule_id UUID REFERENCES schedules(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS sessions_schedule_id_idx ON sessions(schedule_id) WHERE schedule_id IS NOT NULL;
