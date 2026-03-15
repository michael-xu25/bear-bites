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

import logging
import os
import time
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
# Step 3 — Sync today's menu into Supabase daily_menus
# ---------------------------------------------------------------------------

# Rows are batched to stay well under PostgREST's default request-size limit.
_BATCH_SIZE = 400


def sync_daily_menu(sb: Client, entries: list[dict], today: str = TODAY) -> None:
    """
    Persist today's parsed menu entries into the daily_menus table so the
    iOS app can query Supabase instead of hitting the Brown Dining API directly.

    Two-phase approach:
      1. DELETE rows whose date is strictly before today, keeping the table
         lean (one day of data is all the iOS app ever needs).
      2. INSERT today's rows in batches of _BATCH_SIZE, using
         ON CONFLICT DO NOTHING so re-running the worker mid-day is safe.
    """
    # ── Phase 1: prune stale rows ────────────────────────────────────────────
    prune_resp = (
        sb.table("daily_menus")
        .delete()
        .lt("date", today)   # strictly less than today → removes yesterday and older
        .execute()
    )
    pruned = len(prune_resp.data) if prune_resp.data else 0
    if pruned:
        log.info("Pruned %d stale daily_menus row(s) from before %s.", pruned, today)

    if not entries:
        log.warning("No menu entries to sync for today (%s).", today)
        return

    # ── Phase 2: insert today's menu ─────────────────────────────────────────
    rows = [
        {
            "date":             today,
            "dining_hall_id":   e["location_id"],
            "dining_hall_name": e["location_name"],
            "meal_period":      e["meal_period"],
            "station":          e["station"],
            "food_item":        e["food_item"],
        }
        for e in entries
    ]

    inserted = 0
    for i in range(0, len(rows), _BATCH_SIZE):
        batch = rows[i : i + _BATCH_SIZE]
        sb.table("daily_menus").upsert(
            batch,
            on_conflict="date,dining_hall_id,meal_period,station,food_item",
        ).execute()
        inserted += len(batch)

    log.info(
        "Synced %d menu item(s) for %s into daily_menus (%d batch(es)).",
        inserted,
        today,
        -(-len(rows) // _BATCH_SIZE),  # ceiling division
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

            url = f"https://api.push.apple.com/3/device/{apn_token}"
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

    # ── 2. Fetch and parse today's dining menu ───────────────────────────────
    locations = fetch_menus()
    menu_entries = parse_todays_menu(locations)

    if not menu_entries:
        log.warning("No menu data found for today (%s). Exiting early.", TODAY)
        return

    menu_index = build_menu_index(menu_entries)

    # ── 3. Sync today's menu into Supabase daily_menus ───────────────────────
    sync_daily_menu(sb, menu_entries)

    # ── 4. Load user favorites from Supabase ─────────────────────────────────
    favorites = load_favorites(sb)

    if not favorites:
        log.info("No favorites stored in the database yet. Nothing to match.")
        return

    # ── 5. Cross-reference ───────────────────────────────────────────────────
    matches = find_matches(favorites, menu_index)

    # ── 6. Send APNs notifications ────────────────────────────────────────────
    apns_key_id      = os.environ.get("APNS_KEY_ID", "").strip()
    apns_team_id     = os.environ.get("APNS_TEAM_ID", "").strip()
    apns_bundle_id   = os.environ.get("APNS_BUNDLE_ID", "").strip()
    apns_private_key = os.environ.get("APNS_PRIVATE_KEY", "").strip()

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
    )

    log.info("Worker finished successfully.")


if __name__ == "__main__":
    main()
