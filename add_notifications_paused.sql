-- Migration: add notifications_paused column to users table
-- Run this in the Supabase SQL editor (Dashboard → SQL Editor → New query).

ALTER TABLE users
ADD COLUMN IF NOT EXISTS notifications_paused BOOLEAN NOT NULL DEFAULT FALSE;
