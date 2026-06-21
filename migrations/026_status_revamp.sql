ALTER TABLE sessions ALTER COLUMN status SET DEFAULT 'idle';
UPDATE sessions SET status = 'idle' WHERE status IN ('active', 'complete');
