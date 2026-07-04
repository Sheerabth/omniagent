-- Store langfuse trace ID on the session so chained turns (defer →
-- follow-up) share one trace instead of creating a new one per job.
-- Cleared when the session reaches a terminal status.
ALTER TABLE sessions ADD COLUMN langfuse_trace_id TEXT;
