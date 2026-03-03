-- ============================================================
-- BearBites — Add daily_menus table
-- Run this in the Supabase SQL Editor.
-- ============================================================


-- ============================================================
-- 1. TABLE
-- One row per recipe item served on a given day.
-- Written exclusively by the Python worker (service_role key).
-- Read by the iOS app (anon key).
-- ============================================================

CREATE TABLE IF NOT EXISTS public.daily_menus (
    id               UUID    PRIMARY KEY DEFAULT gen_random_uuid(),
    date             DATE    NOT NULL,
    dining_hall_id   TEXT    NOT NULL,   -- short location code, e.g. "SHRP"
    dining_hall_name TEXT    NOT NULL,   -- display name, e.g. "Sharpe Refectory"
    meal_period      TEXT    NOT NULL,   -- "Breakfast" | "Lunch" | "Dinner"
    station          TEXT    NOT NULL,   -- e.g. "Soups", "Grill"
    food_item        TEXT    NOT NULL    -- e.g. "Honey Yogurt Greek Chicken"
);

COMMENT ON TABLE  public.daily_menus                  IS 'Pre-parsed daily menu written by the worker; read by the iOS app.';
COMMENT ON COLUMN public.daily_menus.dining_hall_id   IS 'Matches locationId in the Brown Dining API (e.g. SHRP, VW, AC).';
COMMENT ON COLUMN public.daily_menus.meal_period      IS 'Breakfast, Lunch, or Dinner.';


-- ============================================================
-- 2. INDEXES
-- ============================================================

-- The iOS app always queries by today's date.
CREATE INDEX IF NOT EXISTS daily_menus_date_idx
    ON public.daily_menus (date);

-- Filtering by both date and hall (e.g. "show me the Ratty menu") is common.
CREATE INDEX IF NOT EXISTS daily_menus_date_hall_idx
    ON public.daily_menus (date, dining_hall_id);

-- Unique constraint on the natural key — prevents duplicate rows when the
-- worker runs more than once in a day and enables ON CONFLICT DO NOTHING.
CREATE UNIQUE INDEX IF NOT EXISTS daily_menus_unique_item
    ON public.daily_menus (date, dining_hall_id, meal_period, station, food_item);


-- ============================================================
-- 3. ROW LEVEL SECURITY
--
-- Read policy  : open to everyone (anon + authenticated).
-- Write policy : none — the service_role key bypasses RLS
--                entirely, so the worker can always write.
--                No INSERT/UPDATE/DELETE policy is created for
--                anon/authenticated, which blocks them from writing.
-- ============================================================

ALTER TABLE public.daily_menus ENABLE ROW LEVEL SECURITY;

CREATE POLICY "daily_menus: anyone can read"
    ON public.daily_menus
    FOR SELECT
    TO anon, authenticated
    USING (true);


-- ============================================================
-- 4. QUICK SANITY CHECK (optional — safe to delete)
-- ============================================================

-- SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public';
-- SELECT policyname, tablename, cmd FROM pg_policies WHERE schemaname = 'public';
