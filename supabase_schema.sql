-- ============================================================
-- BearBites — Supabase PostgreSQL Schema
-- Run this entire file in the Supabase SQL Editor.
-- ============================================================


-- ============================================================
-- 0. EXTENSIONS
-- uuid_generate_v4() lives in pgcrypto (available by default
-- in Supabase). Enable it just in case.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS "pgcrypto";


-- ============================================================
-- 1. USERS
-- One row per physical device. The iOS app creates this row
-- on first launch using a silent anonymous sign-in, then
-- upserts the APN token whenever it refreshes.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.users (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ NOT NULL    DEFAULT now(),
    apn_token   TEXT        UNIQUE      -- Apple Push Notification device token
);

COMMENT ON TABLE  public.users           IS 'One row per iOS device / anonymous user.';
COMMENT ON COLUMN public.users.apn_token IS 'APNs device token; refreshed by the iOS app on every launch.';


-- ============================================================
-- 2. FAVORITES
-- Each row is a (user, food_item) pair the user wants alerts
-- for, optionally scoped to a specific dining hall.
-- ============================================================

CREATE TABLE IF NOT EXISTS public.favorites (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at     TIMESTAMPTZ NOT NULL    DEFAULT now(),
    user_id        UUID        NOT NULL    REFERENCES public.users (id) ON DELETE CASCADE,
    food_item      TEXT        NOT NULL,   -- e.g. "Honey Yogurt Greek Chicken"
    dining_hall_id TEXT        NULL        -- e.g. "SHRP"; NULL = alert from any hall
);

-- Fast lookup: "give me all favorites for user X"
CREATE INDEX IF NOT EXISTS favorites_user_id_idx ON public.favorites (user_id);

-- Prevent the same (user, food, hall) triple being inserted twice
CREATE UNIQUE INDEX IF NOT EXISTS favorites_unique_per_user
    ON public.favorites (user_id, food_item, dining_hall_id)
    NULLS NOT DISTINCT; -- treat NULL dining_hall_id as equal for dedup purposes

COMMENT ON TABLE  public.favorites              IS 'Meals a user wants push alerts for.';
COMMENT ON COLUMN public.favorites.food_item    IS 'Exact recipe name as it appears in the Brown Dining API (item field).';
COMMENT ON COLUMN public.favorites.dining_hall_id IS 'Optional locationId filter (e.g. SHRP). NULL means any hall.';


-- ============================================================
-- 3. ROW LEVEL SECURITY
--
-- Strategy: anonymous device-ID auth (no email/password).
-- The iOS app calls supabase.auth.signInAnonymously() on first
-- launch. Supabase creates a JWT whose `sub` claim equals the
-- user's UUID. The app stores that UUID in the users table
-- as the primary key, so auth.uid() == users.id always holds.
--
-- This avoids the App Store "Demo Account" rejection because
-- there is no login screen — auth is fully invisible to the user.
-- ============================================================

ALTER TABLE public.users     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.favorites ENABLE ROW LEVEL SECURITY;


-- ── users policies ──────────────────────────────────────────

-- A device may insert its own row (id must match the JWT sub).
CREATE POLICY "users: device can insert own row"
    ON public.users
    FOR INSERT
    TO anon, authenticated
    WITH CHECK (id = auth.uid());

-- A device may read its own row.
CREATE POLICY "users: device can read own row"
    ON public.users
    FOR SELECT
    TO anon, authenticated
    USING (id = auth.uid());

-- A device may update its own APN token (and only its own row).
CREATE POLICY "users: device can update own row"
    ON public.users
    FOR UPDATE
    TO anon, authenticated
    USING     (id = auth.uid())
    WITH CHECK (id = auth.uid());

-- A device may delete its own row ("Delete my account").
CREATE POLICY "users: device can delete own row"
    ON public.users
    FOR DELETE
    TO anon, authenticated
    USING (id = auth.uid());


-- ── favorites policies ───────────────────────────────────────

-- A device may insert favorites only for itself.
CREATE POLICY "favorites: device can insert own favorites"
    ON public.favorites
    FOR INSERT
    TO anon, authenticated
    WITH CHECK (user_id = auth.uid());

-- A device may read its own favorites.
CREATE POLICY "favorites: device can read own favorites"
    ON public.favorites
    FOR SELECT
    TO anon, authenticated
    USING (user_id = auth.uid());

-- A device may update its own favorites (e.g. change dining_hall_id).
CREATE POLICY "favorites: device can update own favorites"
    ON public.favorites
    FOR UPDATE
    TO anon, authenticated
    USING     (user_id = auth.uid())
    WITH CHECK (user_id = auth.uid());

-- A device may delete its own favorites (un-favorite a meal).
CREATE POLICY "favorites: device can delete own favorites"
    ON public.favorites
    FOR DELETE
    TO anon, authenticated
    USING (user_id = auth.uid());


-- ── service-role bypass (for the Python backend worker) ─────
-- The Python notification worker uses the service_role key,
-- which bypasses RLS entirely — no extra policy needed.
-- Never expose the service_role key in the iOS app.


-- ============================================================
-- 4. QUICK SANITY CHECK (optional — safe to delete)
-- After running the schema, run these SELECTs to confirm the
-- tables and policies exist.
-- ============================================================

-- SELECT tablename, rowsecurity FROM pg_tables
--   WHERE schemaname = 'public';

-- SELECT policyname, tablename, cmd, roles
--   FROM pg_policies
--   WHERE schemaname = 'public'
--   ORDER BY tablename, cmd;
