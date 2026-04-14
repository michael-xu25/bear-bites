-- Run this in the Supabase dashboard SQL editor (Database → SQL Editor).
--
-- Creates a suggestions table so app users can submit feature ideas.
-- The iOS app inserts using the anon key. Only the service_role key
-- (dashboard / worker) can read rows — submissions are private to the developer.

CREATE TABLE IF NOT EXISTS suggestions (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at      TIMESTAMPTZ NOT NULL    DEFAULT now(),
    suggestion      TEXT        NOT NULL    CHECK (length(trim(suggestion)) > 0),
    submitter_name  TEXT,
    device_id       UUID        REFERENCES users(id) ON DELETE SET NULL
);

-- Index for browsing newest first in the Supabase dashboard.
CREATE INDEX IF NOT EXISTS suggestions_created_at_idx ON suggestions (created_at DESC);

-- Enable RLS.
ALTER TABLE suggestions ENABLE ROW LEVEL SECURITY;

-- Anyone (anon iOS app) can insert — checked that the text is non-empty.
CREATE POLICY "Anyone can submit a suggestion"
    ON suggestions
    FOR INSERT
    TO anon
    WITH CHECK (length(trim(suggestion)) > 0);

-- Only service_role can read (developer views via Supabase dashboard).
-- No SELECT policy for anon — submissions are not exposed back to the app.
