"""
worker.py — BearBites daily notification worker.

Intended to run once per day (e.g., 7 AM via cron, GitHub Actions, or
Google Cloud Scheduler). Execution order:

  1. Fetch the Brown Dining API JSON (~2.5 MB, covers all halls for a week).
  2. Parse every recipe item being served *today* across all dining halls.
  3. Sync today's menu into the Supabase daily_menus table so the iOS app
     can read it without ever hitting the Brown API directly.
  4. Load all user favorites from Supabase (uses service_role key -> bypasses RLS).
  5. Cross-reference favorites against today's menu with hall-scoping logic.
  6. Send APNs push notifications for each match.

Required environment variables:
  SUPABASE_URL   -- your project URL, e.g. https://xyzxyz.supabase.co
  SUPABASE_KEY   -- the *service_role* secret key (never the anon key).
                   The service_role key bypasses Row Level Security so the
                   worker can read every user's favorites and APN tokens,
                   and write to daily_menus.

APNs environment variables (all four required for real notifications):
  APNS_KEY_ID      -- 10-char Key ID from Apple Developer portal
  APNS_TEAM_ID     -- 10-char Team ID from Apple Developer portal
  APNS_BUNDLE_ID   -- app bundle ID, e.g. Bricked-Labs.Bear-Bites
  APNS_PRIVATE_KEY -- full contents of the .p8 file (including header/footer)

Optional (for local dev):
  Place a .env file in the same directory and install python-dotenv.
  The script calls load_dotenv() before reading os.environ.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

# python-dotenv is optional — only loaded when present.
# Install it for local development: `pip install python-dotenv`
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import httpx
import jwt as pyjwt  # PyJWT — pip install PyJWT
from supabase import Client, create_client

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DINING_API_URL = "https://esb-level1.brown.edu/services/oit/sys/brown-dining/v1/menus"

# ISO date string for today in US/Eastern time, e.g. "2026-03-03".
# Always use Eastern time regardless of where the worker runs (local Mac,
# GitHub Actions UTC, etc.) — Brown Dining is on the Brown campus and the
# iOS app uses the device's local Eastern date for its Supabase query.
TODAY: str = datetime.now(ZoneInfo("America/New_York")).date().isoformat()

# ---------------------------------------------------------------------------
# Meal timing
# ---------------------------------------------------------------------------

# Approximate Brown Dining meal start times (US/Eastern).
# The Brown Dining API does not include schedule times, so these are
# hardcoded based on typical hall hours.
MEAL_START_TIMES_ET: dict[str, tuple[int, int]] = {
    "Breakfast": (7, 30),
    "Lunch":     (11, 0),
    "Dinner":    (17, 0),
}

# Notification window: send if the meal starts within this many minutes.
# The meal-time cron triggers fire ~5 min before each meal; the window
# is wider to absorb GitHub Actions scheduling jitter.
_NOTIFY_WINDOW_MINUTES = 20


def get_upcoming_meal_period() -> str | None:
    """
    Return the meal period name whose start time is within the next
    _NOTIFY_WINDOW_MINUTES minutes (or up to 5 minutes past), or None
    if no meal is starting soon.

    Used to decide whether to dispatch notifications and which meal to
    filter matches to.  Returns None during the 2 AM menu-sync run.
    """
    now = datetime.now(ZoneInfo("America/New_York"))
    for period, (h, m) in MEAL_START_TIMES_ET.items():
        start = now.replace(hour=h, minute=m, second=0, microsecond=0)
        delta_minutes = (start - now).total_seconds() / 60
        if -5 <= delta_minutes <= _NOTIFY_WINDOW_MINUTES:
            return period
    return None


# ---------------------------------------------------------------------------
# Step 1 — Fetch the Brown Dining API
# ---------------------------------------------------------------------------


def fetch_menus(url: str = DINING_API_URL, max_attempts: int = 5) -> list:
    """
    Download and decode the Brown Dining JSON payload (~2.5 MB).

    Retries on transient failures (timeouts, 5xx, connection errors) with
    exponential backoff so a flaky network or brief Brown API hiccup does
    not fail the whole GitHub Actions / cron run.

    Returns the top-level list of location objects (one per dining hall).
    """
    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            log.info("Fetching Brown Dining API (attempt %d/%d): %s", attempt, max_attempts, url)
            # Large JSON — allow a generous read timeout.
            response = requests.get(url, timeout=(15, 120))
            response.raise_for_status()
            locations: list = response.json()
            log.info("Received %d location object(s) from the API.", len(locations))
            return locations
        except (requests.HTTPError, requests.ConnectionError, requests.Timeout) as exc:
            last_error = exc
            status = getattr(exc.response, "status_code", None) if isinstance(exc, requests.HTTPError) else None
            if status is not None and status < 500 and status != 429:
                raise
            wait = min(2**attempt, 60)
            log.warning(
                "Brown API fetch failed (attempt %d/%d): %s — retrying in %ds",
                attempt,
                max_attempts,
                exc,
                wait,
            )
            if attempt < max_attempts:
                time.sleep(wait)
        except ValueError as exc:
            # response.json() failed — bad payload; retry once more in case of partial read.
            last_error = exc
            wait = min(2**attempt, 30)
            log.warning("JSON decode failed (attempt %d/%d): %s — retrying in %ds", attempt, max_attempts, exc, wait)
            if attempt < max_attempts:
                time.sleep(wait)

    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# Step 2 — Parse the full week's menus
# ---------------------------------------------------------------------------


def parse_week_menus(locations: list) -> list[dict]:
    """
    Walk the full API payload and return a flat list of every *recipe* item
    across ALL date keys present in the response (today + upcoming days).

    Each element in the returned list is a dict with these keys:
        date          — "YYYY-MM-DD" string from the API key   (str)
        food_item     — canonical item name from the API        (str)
        location_id   — short hall ID, e.g. "SHRP"             (str)
        location_name — display name, e.g. "Sharpe Refectory"  (str)
        meal_period   — "Breakfast", "Lunch", or "Dinner"       (str)
        station       — station name, e.g. "Soups"              (str)

    Items whose itemType != "recipe" are skipped (filters out raw
    ingredients like "Butter" and "Salt").
    """
    entries: list[dict] = []

    for location in locations:
        loc_id: str = location.get("locationId", "UNKNOWN")
        loc_name: str = location.get("name", "Unknown Hall")
        meals_by_date: dict = location.get("meals", {})

        for date_key, day_meals in meals_by_date.items():
            if not day_meals:
                continue

            for meal_period_obj in day_meals:
                period: str = meal_period_obj.get("meal", "Unknown")
                stations: list = meal_period_obj.get("menu", {}).get("stations", [])

                for station in stations:
                    station_name: str = station.get("name", "Unknown Station")

                    for item in station.get("items", []):
                        if item.get("itemType") != "recipe":
                            continue

                        food_name: str = item.get("item", "").strip()
                        if not food_name:
                            continue

                        entries.append(
                            {
                                "date": date_key,
                                "food_item": food_name,
                                "location_id": loc_id,
                                "location_name": loc_name,
                                "meal_period": period,
                                "station": station_name,
                            }
                        )

    dates_found = sorted({e["date"] for e in entries})
    log.info(
        "Parsed %d recipe item(s) across %d date(s): %s",
        len(entries),
        len(dates_found),
        ", ".join(dates_found),
    )
    return entries


def build_menu_index(entries: list[dict]) -> dict:
    """
    Convert the flat list of menu entries into a two-level lookup dict for
    O(1) matching during the cross-reference step.

    Structure:
        {
          food_item_lowercased: {
            location_id: [ entry_dict, ... ],
            ...
          }
        }

    The outer key is lowercased so comparisons against favorites are
    case-insensitive — the API and user-entered names may differ in casing.
    Multiple entries can share the same (food, hall) pair when the same dish
    appears at more than one station or meal period.
    """
    index: dict = defaultdict(lambda: defaultdict(list))

    for entry in entries:
        key = entry["food_item"].lower()
        index[key][entry["location_id"]].append(entry)

    return index


# ---------------------------------------------------------------------------
# Step 3 — Sync the full week's menus into Supabase daily_menus
# ---------------------------------------------------------------------------

# Rows are batched to stay well under PostgREST's default request-size limit.
_BATCH_SIZE = 400


def sync_daily_menu(sb: Client, entries: list[dict]) -> None:
    """
    Persist all parsed menu entries (today + upcoming days from the API) into
    daily_menus, maintaining a rolling ~14-day window visible in the Discover
    catalog: the past 7 days of history plus today plus ~6 upcoming days.

    Three-phase approach:
      1. DELETE rows older than 7 days (keeps the past-7-days history window).
      2. For each date present in the new API data, DELETE its existing rows
         so the fresh insert is a full replacement. This handles menu changes
         (dishes swapped out, new items added) for any date — including future
         dates that were saved by an earlier worker run.
      3. INSERT all entries in batches of _BATCH_SIZE.
    """
    if not entries:
        log.warning("No menu entries to sync.")
        return

    # ── Phase 1: prune rows older than 7 days ────────────────────────────────
    seven_days_ago = (
        datetime.now(ZoneInfo("America/New_York")) - timedelta(days=7)
    ).date().isoformat()

    prune_resp = (
        sb.table("daily_menus")
        .delete()
        .lt("date", seven_days_ago)
        .execute()
    )
    pruned = len(prune_resp.data) if prune_resp.data else 0
    if pruned:
        log.info("Pruned %d stale daily_menus row(s) from before %s.", pruned, seven_days_ago)

    # ── Phase 2: delete existing rows for every date we are about to insert ──
    # This is a full replacement per date: stale entries (dishes removed from
    # the menu since the last run) are cleared before the fresh data goes in.
    dates_to_sync = sorted({e["date"] for e in entries})
    for date in dates_to_sync:
        sb.table("daily_menus").delete().eq("date", date).execute()
    log.info(
        "Cleared existing rows for %d date(s) before re-inserting: %s",
        len(dates_to_sync),
        ", ".join(dates_to_sync),
    )

    # ── Phase 3: insert fresh rows for all dates ──────────────────────────────
    # Deduplicate before inserting. Two entries are considered the same dish if
    # they share (date, dining_hall, meal_period, food_item) — station is
    # intentionally excluded so a dish listed at multiple stations on the same
    # day/meal collapses to one row. Entries on different dates or in different
    # meal periods are always kept as separate rows so notifications fire
    # independently for each applicable day and meal time.
    seen_keys: set[tuple] = set()
    rows: list[dict] = []
    for e in entries:
        key = (e["date"], e["location_id"], e["meal_period"], e["food_item"])
        if key in seen_keys:
            continue
        seen_keys.add(key)
        rows.append(
            {
                "date":             e["date"],
                "dining_hall_id":   e["location_id"],
                "dining_hall_name": e["location_name"],
                "meal_period":      e["meal_period"],
                "station":          e["station"],
                "food_item":        e["food_item"],
            }
        )

    inserted = 0
    for i in range(0, len(rows), _BATCH_SIZE):
        batch = rows[i : i + _BATCH_SIZE]
        sb.table("daily_menus").insert(batch).execute()
        inserted += len(batch)

    log.info(
        "Synced %d menu item(s) across %d date(s) into daily_menus (%d batch(es)).",
        inserted,
        len(dates_to_sync),
        -(-len(rows) // _BATCH_SIZE),
    )


# ---------------------------------------------------------------------------
# Step 4 — Load favorites from Supabase
# ---------------------------------------------------------------------------


def load_favorites(sb: Client) -> list[dict]:
    """
    Fetch every row from the favorites table and enrich each row with the
    corresponding user's APN token from the users table.

    The service_role key bypasses RLS, so this returns records for every user
    — not just the authenticated caller.

    Returns a list of dicts:
        user_id        — UUID string
        apn_token      — APNs device token string, or None if not yet registered
        food_item      — the recipe name the user wants alerts for
        dining_hall_id — locationId string (e.g. "SHRP"), or None for any hall
    """
    log.info("Loading favorites from Supabase...")

    fav_response = (
        sb.table("favorites")
        .select("user_id, food_item, dining_hall_id")
        .execute()
    )
    favorites: list[dict] = fav_response.data or []

    if not favorites:
        log.info("Favorites table is empty — nothing to match.")
        return []

    log.info("Found %d favorite row(s) across all users.", len(favorites))

    # Collect the unique set of user IDs referenced by the favorites rows,
    # then fetch their APN tokens in a single query.
    user_ids = list({row["user_id"] for row in favorites})
    user_response = (
        sb.table("users")
        .select("id, apn_token")
        .in_("id", user_ids)
        .execute()
    )
    # Build a { user_id → apn_token } map for O(1) lookup.
    token_map: dict[str, str | None] = {
        u["id"]: u.get("apn_token") for u in (user_response.data or [])
    }

    # Merge the APN token into each favorites row.
    enriched: list[dict] = []
    for row in favorites:
        enriched.append(
            {
                "user_id": row["user_id"],
                "apn_token": token_map.get(row["user_id"]),
                "food_item": row["food_item"],
                "dining_hall_id": row.get("dining_hall_id"),  # None = any hall
            }
        )

    return enriched


# ---------------------------------------------------------------------------
# Step 4 — Cross-reference favorites against today's menu
# ---------------------------------------------------------------------------


def find_matches(favorites: list[dict], menu_index: dict) -> list[dict]:
    """
    For each favorite, look up the food item in today's menu index and apply
    the hall-scoping rule:

        dining_hall_id is NOT NULL  →  only match if served at *that* hall.
        dining_hall_id IS NULL      →  match if served at *any* hall.

    Returns a flat list of match dicts, one per (user × food_item × hall × meal_period):
        apn_token, user_id, food_item, location_id, location_name, meal_period
    """
    matches: list[dict] = []

    for fav in favorites:
        food_lower = fav["food_item"].lower()
        hall_filter: str | None = fav["dining_hall_id"]

        # Fast lookup — key miss means the item simply isn't on today's menu.
        hall_hits: dict = menu_index.get(food_lower, {})
        if not hall_hits:
            continue

        if hall_filter:
            # User scoped this favorite to a specific dining hall.
            candidate_entries = hall_hits.get(hall_filter, [])
        else:
            # User accepts this item from any dining hall — collect all entries.
            candidate_entries = [
                entry
                for hall_entries in hall_hits.values()
                for entry in hall_entries
            ]

        for entry in candidate_entries:
            matches.append(
                {
                    "apn_token": fav["apn_token"],
                    "user_id": fav["user_id"],
                    # Use the canonical casing from the API, not the user's stored string.
                    "food_item": entry["food_item"],
                    "location_id": entry["location_id"],
                    "location_name": entry["location_name"],
                    "meal_period": entry["meal_period"],
                }
            )

    return matches


# ---------------------------------------------------------------------------
# Step 5 — Log matches (APN dispatch placeholder)
# ---------------------------------------------------------------------------


def _build_apns_jwt(key_id: str, team_id: str, private_key_pem: str) -> str:
    """
    Build a short-lived ES256 JWT for APNs token-based authentication.
    Valid for 1 hour (APNs rejects tokens older than 60 minutes).
    """
    payload = {
        "iss": team_id,
        "iat": int(time.time()),
    }
    headers = {
        "alg": "ES256",
        "kid": key_id,
    }
    return pyjwt.encode(payload, private_key_pem, algorithm="ES256", headers=headers)


def send_notifications(
    matches: list[dict],
    key_id: str,
    team_id: str,
    bundle_id: str,
    private_key_pem: str,
    dispatch_enabled: bool = True,
    sandbox: bool = True,
) -> None:
    """
    Deduplicate matches and send one APNs push notification per unique match.

    Deduplication key: (user_id, food_item, location_id, meal_period).
    This prevents double-firing when the same dish appears at two stations
    during the same meal period.

    Matches with no APNs token are logged as warnings and skipped — those
    devices haven't granted notification permission yet.
    """
    if not matches:
        log.info("No matches found for today (%s). No notifications to send.", TODAY)
        return

    # Deduplicate.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for m in matches:
        key = (m["user_id"], m["food_item"].lower(), m["location_id"], m["meal_period"])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    log.info("Total unique match(es) to notify: %d", len(unique))

    # Log the match summary regardless of whether tokens are available.
    separator = "=" * 72
    print()
    print(separator)
    print(f"  BEARBITES MATCH LOG — {TODAY}")
    print(separator)
    for m in unique:
        token_display = m["apn_token"] if m["apn_token"] else "(no APN token yet)"
        print(
            f"  MATCH FOUND: [{token_display}] "
            f"for [{m['food_item']}] "
            f"at [{m['location_name']} ({m['location_id']})] "
            f"— {m['meal_period']}"
        )
    print(separator)
    print()

    if not dispatch_enabled:
        log.info("APNs dispatch disabled — match log printed above, no notifications sent.")
        return

    # Build JWT once — reused for all notifications in this run.
    token = _build_apns_jwt(key_id, team_id, private_key_pem)

    apns_host = "api.sandbox.push.apple.com" if sandbox else "api.push.apple.com"
    log.info("APNs host: %s", apns_host)

    sent = 0
    skipped = 0
    failed = 0

    # httpx client with HTTP/2 enabled — APNs requires HTTP/2.
    with httpx.Client(http2=True) as client:
        for m in unique:
            apn_token = m.get("apn_token")
            if not apn_token:
                log.warning(
                    "No APNs token for user %s — skipping \"%s\".",
                    m["user_id"],
                    m["food_item"],
                )
                skipped += 1
                continue

            url = f"https://{apns_host}/3/device/{apn_token}"
            headers = {
                "authorization": f"bearer {token}",
                "apns-topic": bundle_id,
                "apns-push-type": "alert",
                "apns-priority": "10",
            }
            payload = {
                "aps": {
                    "alert": {
                        "title": f"{m['food_item']} is on the menu!",
                        "body": (
                            f"{m['location_name']} is serving it for "
                            f"{m['meal_period'].lower()} today."
                        ),
                    },
                    "sound": "default",
                }
            }

            try:
                resp = client.post(url, json=payload, headers=headers, timeout=10)
                if resp.status_code == 200:
                    log.info(
                        "Notification sent: \"%s\" -> %s...  [%s]",
                        m["food_item"],
                        apn_token[:8],
                        m["meal_period"],
                    )
                    sent += 1
                else:
                    log.error(
                        "APNs rejected notification for token %s...: %s %s",
                        apn_token[:8],
                        resp.status_code,
                        resp.text,
                    )
                    failed += 1
            except httpx.RequestError as exc:
                log.error("Network error sending APNs notification: %s", exc)
                failed += 1

    log.info(
        "Notification summary: %d sent, %d skipped (no token), %d failed.",
        sent,
        skipped,
        failed,
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    # ── 0. Read and validate environment variables ───────────────────────────
    supabase_url = os.environ.get("SUPABASE_URL", "").strip()
    supabase_key = os.environ.get("SUPABASE_KEY", "").strip()

    if not supabase_url or not supabase_key:
        raise EnvironmentError(
            "Both SUPABASE_URL and SUPABASE_KEY must be set as environment variables.\n"
            "Use the *service_role* key — not the anon key — so the worker can read\n"
            "all users' favorites without being blocked by Row Level Security."
        )

    # ── 1. Connect to Supabase ───────────────────────────────────────────────
    sb: Client = create_client(supabase_url, supabase_key)
    log.info("Supabase client initialised (project: %s).", supabase_url)

    # ── 2. Fetch and parse the full week's dining menus ─────────────────────
    locations = fetch_menus()
    all_entries = parse_week_menus(locations)

    if not all_entries:
        log.warning("API returned no recipe rows — nothing to sync. Exiting.")
        return

    # ── 3. Sync full week into Supabase daily_menus ───────────────────────────
    # Always sync whenever the API returned data, even if today's date key is
    # missing (week rollover edge case, holiday, or API glitch). Previously we
    # returned before sync when `todays_entries` was empty, which skipped the
    # entire DB update and broke the Discover tab until the next successful run.
    sync_daily_menu(sb, all_entries)

    # Split: today's entries drive notifications only.
    todays_entries = [e for e in all_entries if e["date"] == TODAY]

    if not todays_entries:
        log.warning(
            "No menu data for today (%s) — DB updated for other dates; skipping notifications.",
            TODAY,
        )
        return

    menu_index = build_menu_index(todays_entries)

    # ── 4. Load user favorites from Supabase ─────────────────────────────────
    favorites = load_favorites(sb)

    if not favorites:
        log.info("No favorites stored in the database yet. Nothing to match.")
        return

    # ── 5. Cross-reference ───────────────────────────────────────────────────
    matches = find_matches(favorites, menu_index)

    # ── 6. Filter matches to the upcoming meal period ─────────────────────────
    # FORCE_NOTIFY=true is set by cron-job.org triggers, which fire at exact
    # meal times. In that case skip the time-window check and use whichever
    # meal period is closest to now instead of requiring it to be within the
    # narrow window (which could fail if GitHub job startup adds latency).
    force_notify = os.environ.get("FORCE_NOTIFY", "").strip().lower() == "true"

    if force_notify:
        # Find the nearest meal period by absolute time distance.
        now = datetime.now(ZoneInfo("America/New_York"))
        def _minutes_away(period_name: str) -> float:
            h, m = MEAL_START_TIMES_ET[period_name]
            start = now.replace(hour=h, minute=m, second=0, microsecond=0)
            return abs((start - now).total_seconds() / 60)
        meal_period = min(MEAL_START_TIMES_ET, key=_minutes_away)
        log.info(
            "FORCE_NOTIFY=true — nearest meal period: %s. "
            "Sending notifications for %d match(es).",
            meal_period,
            len([m for m in matches if m["meal_period"] == meal_period]),
        )
    else:
        meal_period = get_upcoming_meal_period()

    if meal_period:
        matches = [m for m in matches if m["meal_period"] == meal_period]
        log.info(
            "Meal period: %s. Filtered to %d match(es) for notification.",
            meal_period,
            len(matches),
        )
    else:
        log.info(
            "No meal period starting soon — menu synced, notifications skipped."
        )
        log.info("Worker finished successfully.")
        return

    # ── 7. Send APNs notifications ────────────────────────────────────────────
    apns_key_id      = os.environ.get("APNS_KEY_ID", "").strip()
    apns_team_id     = os.environ.get("APNS_TEAM_ID", "").strip()
    apns_bundle_id   = os.environ.get("APNS_BUNDLE_ID", "").strip()
    apns_private_key = os.environ.get("APNS_PRIVATE_KEY", "").strip()
    # APNS_SANDBOX=true  → api.sandbox.push.apple.com  (Xcode dev builds)
    # APNS_SANDBOX=false → api.push.apple.com          (App Store / TestFlight)
    apns_sandbox     = os.environ.get("APNS_SANDBOX", "true").strip().lower() == "true"

    # GitHub Actions passes multiline secrets with real newlines; .env files
    # store them as literal \n. Normalise both to real newlines here.
    apns_private_key = apns_private_key.replace("\\n", "\n")

    apns_configured = all([apns_key_id, apns_team_id, apns_bundle_id, apns_private_key])

    if not apns_configured:
        log.warning(
            "APNs env vars not fully set (APNS_KEY_ID, APNS_TEAM_ID, "
            "APNS_BUNDLE_ID, APNS_PRIVATE_KEY). Logging matches only — "
            "no notifications will be sent."
        )

    send_notifications(
        matches,
        key_id=apns_key_id,
        team_id=apns_team_id,
        bundle_id=apns_bundle_id,
        private_key_pem=apns_private_key,
        dispatch_enabled=apns_configured,
        sandbox=apns_sandbox,
    )

    log.info("Worker finished successfully.")


if __name__ == "__main__":
    main()
