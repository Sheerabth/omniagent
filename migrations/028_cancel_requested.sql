-- status is job-owned (only the worker writes it) except for 'cancelled',
-- which the API used to write directly — creating a race where 'cancelled'
-- could mean "requested" or "confirmed" depending on timing. Split the
-- concepts: cancel_requested is the API's request, status stays job-owned.
ALTER TABLE sessions ADD COLUMN cancel_requested boolean NOT NULL DEFAULT false;
