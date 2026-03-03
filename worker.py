"""
worker.py — BearBites daily notification worker.

Intended to run once per day (e.g., 7 AM via cron, GitHub Actions, or
Google Cloud Scheduler). Execution order:

  1. Fetch the Brown Dining API JSON (~2.5 MB, covers all halls for a week).
  2. Parse every recipe item being served *today* across all dining halls.
  3. Load all user favorites from Supabase (uses service_role key → bypasses RLS).
  4. Cross-reference favorites against today's menu with hall-scoping logic.
  5. Print a match log. (Real APNs dispatch replaces the print() calls in Phase 4.)

Required environment variables:
  SUPABASE_URL   — your project URL, e.g. https://xyzxyz.supabase.co
  SUPABASE_KEY   — the *service_role* secret key (never the anon key).
                   The service_role key bypasses Row Level Security so the
                   worker can read every user's favorites and APN tokens.

Optional (for local dev):
  Place a .env file in the same directory and install python-dotenv.
  The script calls load_dotenv() before reading os.environ.
"""

import logging
import os
from collections import defaultdict
from datetime import date

import requests

# python-dotenv is optional — only loaded when present.
# Install it for local development: `pip install python-dotenv`
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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

# ISO date string for today in local time, e.g. "2026-03-03".
# The Brown Dining API keys its meal data by date, so this must match the
# server timezone.  The worker should be deployed in the US/Eastern timezone
# or the date should be passed in explicitly for correctness.
TODAY: str = date.today().isoformat()


# ---------------------------------------------------------------------------
# Step 1 — Fetch the Brown Dining API
# ---------------------------------------------------------------------------


def fetch_menus(url: str = DINING_API_URL) -> list:
    """
    Download and decode the Brown Dining JSON payload.

    Returns the top-level list of location objects (one per dining hall).
    Raises requests.HTTPError on a non-2xx response and requests.ConnectionError
    on network failure — both should be caught and retried by the caller
    or the surrounding scheduler.
    """
    log.info("Fetching Brown Dining API: %s", url)
    response = requests.get(url, timeout=30)
    response.raise_for_status()
    locations: list = response.json()
    log.info("Received %d location object(s) from the API.", len(locations))
    return locations


# ---------------------------------------------------------------------------
# Step 2 — Parse today's menu
# ---------------------------------------------------------------------------


def parse_todays_menu(locations: list, today: str = TODAY) -> list[dict]:
    """
    Walk the full API payload and return a flat list of every *recipe* item
    being served today across all dining halls and all meal periods.

    Each element in the returned list is a dict with these keys:
        food_item     — canonical item name from the API  (str)
        location_id   — short hall ID, e.g. "SHRP"       (str)
        location_name — display name, e.g. "Sharpe Refectory" (str)
        meal_period   — "Breakfast", "Lunch", or "Dinner" (str)
        station       — station name, e.g. "Soups"        (str)

    Items whose itemType != "recipe" are skipped; this filters out raw
    ingredients that appear on the line e.g. "Butter", "Salt" etc.
    """
    entries: list[dict] = []

    for location in locations:
        loc_id: str = location.get("locationId", "UNKNOWN")
        loc_name: str = location.get("name", "Unknown Hall")

        # The API payload spans a full week; grab only today's date key.
        day_meals: list = location.get("meals", {}).get(today, [])

        if not day_meals:
            log.debug("No meals for %s (%s) on %s — skipping.", loc_name, loc_id, today)
            continue

        for meal_period_obj in day_meals:
            period: str = meal_period_obj.get("meal", "Unknown")  # Breakfast/Lunch/Dinner
            stations: list = meal_period_obj.get("menu", {}).get("stations", [])

            for station in stations:
                station_name: str = station.get("name", "Unknown Station")

                for item in station.get("items", []):
                    # Skip raw ingredients — we only care about named recipes.
                    if item.get("itemType") != "recipe":
                        continue

                    food_name: str = item.get("item", "").strip()
                    if not food_name:
                        continue

                    entries.append(
                        {
                            "food_item": food_name,
                            "location_id": loc_id,
                            "location_name": loc_name,
                            "meal_period": period,
                            "station": station_name,
                        }
                    )

    log.info(
        "Parsed %d recipe item(s) being served today (%s).", len(entries), today
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
# Step 3 — Load favorites from Supabase
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


def log_matches(matches: list[dict]) -> None:
    """
    Deduplicate and print a structured match log.

    Deduplication key: (user_id, food_item, location_id, meal_period).
    This prevents double-firing when the same dish appears at two stations
    during the same meal period.

    In Phase 4 each `print()` line is replaced by an HTTP call to the
    APNs provider API or FCM batch endpoint.
    """
    if not matches:
        log.info("No matches found for today (%s). No notifications to send.", TODAY)
        return

    # Deduplicate before counting/printing.
    seen: set[tuple] = set()
    unique: list[dict] = []
    for m in matches:
        key = (m["user_id"], m["food_item"].lower(), m["location_id"], m["meal_period"])
        if key not in seen:
            seen.add(key)
            unique.append(m)

    log.info("Total unique match(es) to notify: %d", len(unique))

    separator = "=" * 72
    print()
    print(separator)
    print(f"  BEARBITES MATCH LOG — {TODAY}")
    print(separator)

    for m in unique:
        token_display = m["apn_token"] if m["apn_token"] else "(no APN token yet)"
        print(
            f"  MATCH FOUND: Send APN to [{token_display}] "
            f"for [{m['food_item']}] "
            f"at [{m['location_name']} ({m['location_id']})] "
            f"— {m['meal_period']}"
        )

    print(separator)
    print()


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

    # ── 2. Fetch and parse today's dining menu ───────────────────────────────
    locations = fetch_menus()
    menu_entries = parse_todays_menu(locations)

    if not menu_entries:
        log.warning("No menu data found for today (%s). Exiting early.", TODAY)
        return

    menu_index = build_menu_index(menu_entries)

    # ── 3. Load user favorites from Supabase ─────────────────────────────────
    favorites = load_favorites(sb)

    if not favorites:
        log.info("No favorites stored in the database yet. Nothing to match.")
        return

    # ── 4. Cross-reference ───────────────────────────────────────────────────
    matches = find_matches(favorites, menu_index)

    # ── 5. Print match log (replace with APNs dispatch in Phase 4) ───────────
    log_matches(matches)

    log.info("Worker finished successfully.")


if __name__ == "__main__":
    main()
