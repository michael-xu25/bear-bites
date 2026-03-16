# BearBites
### Never Miss Your Favorite Meal at Brown

BearBites is an iOS app that sends Brown University students a push notification whenever a meal they love is being served at a campus dining hall. Set your favorites once and let BearBites come to you.

---

## Table of Contents

1. [Current State](#current-state)
2. [Tech Stack](#tech-stack)
3. [Project Structure](#project-structure)
4. [System Architecture](#system-architecture)
5. [Database Schema](#database-schema)
6. [Running the Worker](#running-the-worker)
7. [Xcode Guide](#xcode-guide)
8. [Agent Notes: Editing Files](#agent-notes-editing-files)
9. [Remaining Roadmap](#remaining-roadmap)
10. [Dining Hall Reference](#dining-hall-reference)
11. [Brown Dining API Reference](#brown-dining-api-reference)

---

## Current State

### What is built and working

| Component | Status | Description |
|---|---|---|
| Supabase database | Done | 3 tables: `users`, `favorites`, `daily_menus` |
| `worker.py` | Done | Fetches Brown API, parses today's menu, syncs to `daily_menus`, matches favorites, sends APNs push notifications |
| `SupabaseManager.swift` | Done | Shared Supabase client + persistent `DeviceID` |
| `MenuBrowsingView.swift` | Done | Fetches menu from Supabase, grouped by hall + meal period, heart-to-favorite |
| `AddFavoriteView.swift` | Done | Type a food name and save it directly to `favorites` |
| `ItemCatalogView.swift` | Done | Searchable catalog of all menu items seen in the past 7 days — browse by hall, search, heart to favorite, or add custom items via sheet |
| `ContentView.swift` | Done | TabView with Menu tab and Add Favorite tab |

### What is NOT yet built

- Favorites list view (see saved favorites, delete them)
- Real push notification dispatch (working — see Recently fixed)
- Supabase Auth (currently using a `DeviceID` UUID stored in `UserDefaults`)
- APNs token registration
- Search, allergen filters, settings

### Recently fixed / shipped

- **Duplicate menu items:** The same dish appearing at multiple stations within a meal period now shows only once in the list (deduplicated by food name in the `grouped` computed property).
- **Heart persistence:** Tapping a heart now reliably saves to Supabase and survives app relaunches. Fixed two stacked bugs: (1) `MenuBrowsingView` was not calling `registerDevice()` before inserting into `favorites`, causing a silent FK violation; (2) heart state was keyed to daily_menus row UUIDs (which change daily) instead of food item names. Hearts now restore correctly on every launch via `loadFavorites()`. Tapping a hearted item a second time un-favorites it.
- **Push notifications end-to-end:** Real APNs notifications are live and tested. The iOS app requests permission on launch, receives the device token, and uploads it to `users.apn_token`. The worker builds an ES256 JWT from the `.p8` key and POSTs to APNs via HTTP/2 (`httpx`) for each match. Uses the sandbox endpoint for Xcode dev builds (`APNS_SANDBOX=true`, the default) and the production endpoint for App Store/TestFlight (`APNS_SANDBOX=false`).
- **Timezone fix:** Worker now uses US/Eastern time (`America/New_York`) instead of the system/UTC clock so the date always matches the Brown campus and the iOS app.
- **Timed notifications:** Worker now runs 3 additional times per day via GitHub Actions (7:25 AM, 10:55 AM, 4:55 PM EDT) and only dispatches notifications for the meal period starting within the next 20 minutes. The 2 AM run syncs the menu only.
- **Discover catalog:** `daily_menus` now keeps a rolling 7-day window (was pruned to today only). A new Discover tab shows a searchable, hall-grouped catalog of all unique menu items seen over the past week. Users can heart items directly from the catalog or use "Add custom" for items not yet in the DB. The catalog fills up over the first 7 days of use.

---

## Tech Stack

| Layer | Technology |
|---|---|
| iOS Frontend | Swift / SwiftUI (Xcode 16) |
| Push Notifications | Apple Push Notification service (APNs) — not yet wired |
| Backend Worker | Python 3, runs daily via cron / GitHub Actions / Cloud Scheduler |
| Database | Supabase (PostgreSQL) |
| Auth | DeviceID via UserDefaults (real Supabase anonymous auth planned) |
| Source API | Brown Dining Services — `https://esb-level1.brown.edu/services/oit/sys/brown-dining/v1/menus` |

---

## Project Structure

```
BearBites/
├── worker.py                        # Python backend worker
├── requirements.txt                 # Python dependencies
├── .env                             # Local env vars (never committed)
├── supabase_schema.sql              # users + favorites table DDL
├── add_daily_menus_table.sql        # daily_menus table DDL
│
└── BearBitesApp/
    └── Bear Bites/
        ├── Bear_BitesApp.swift      # App entry point
        ├── ContentView.swift        # TabView root
        ├── MenuBrowsingView.swift   # Browse today's menu, tap to favorite
        ├── AddFavoriteView.swift    # Type a food name to add a favorite
        ├── Assets.xcassets/
        └── Services/
            └── SupabaseManager.swift  # Supabase client + DeviceID
```

---

## System Architecture

```
Brown Dining API (2.5 MB JSON, updated daily)
        │
        ▼ (once per day, run worker.py)
┌─────────────────────────────────────────┐
│           worker.py                     │
│  1. Fetch Brown Dining API              │
│  2. Parse today's recipes               │
│  3. Sync → Supabase daily_menus table   │
│  4. Load user favorites from Supabase   │
│  5. Cross-reference favorites vs menu   │
│  6. Print match log (APNs comes later)  │
└─────────────────────────────────────────┘
        │
        ▼
┌─────────────────────┐      ┌──────────────────────────────┐
│   Supabase DB       │      │       iOS App (SwiftUI)      │
│                     │      │                              │
│  users              │◄────►│  MenuBrowsingView            │
│  favorites          │      │    reads daily_menus         │
│  daily_menus        │      │    writes favorites          │
└─────────────────────┘      └──────────────────────────────┘
```

### Why the iOS app never touches the Brown API directly

The raw Brown Dining API payload is ~2.5 MB covering all 7 halls for a full week.

- 1,000 students opening the app at lunch = **2.5 GB of traffic per minute to Brown's servers**
- Risk of rate-limiting or an accidental DDoS on university infrastructure
- Slow app startup parsing 2.5 MB on-device every launch

The worker fetches this **once per day**, parses it, and writes only the relevant structured rows into `daily_menus`. The iOS app reads a tiny, pre-parsed subset — a few KB per query.

---

## Database Schema

### `users`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | Set to `DeviceID.current` from the iOS app |
| `created_at` | TIMESTAMPTZ | Auto |
| `apn_token` | TEXT | Apple Push token — populated later |

### `favorites`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | Auto |
| `created_at` | TIMESTAMPTZ | Auto |
| `user_id` | UUID FK → users | |
| `food_item` | TEXT | Exact recipe name, e.g. "Honey Yogurt Greek Chicken" |
| `dining_hall_id` | TEXT nullable | e.g. "SHRP". NULL = match at any hall |

### `daily_menus`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | Auto |
| `date` | DATE | "YYYY-MM-DD" |
| `dining_hall_id` | TEXT | e.g. "SHRP" |
| `dining_hall_name` | TEXT | e.g. "Sharpe Refectory" |
| `meal_period` | TEXT | "Breakfast", "Lunch", or "Dinner" |
| `station` | TEXT | e.g. "Soups", "Grill" |
| `food_item` | TEXT | e.g. "Honey Yogurt Greek Chicken" |

**RLS rules:**
- `users` + `favorites`: RLS currently **disabled** for development. Re-enable when real auth is added.
- `daily_menus`: RLS **enabled**. Anyone can SELECT. Only the `service_role` key (worker) can write.

---

## Running the Worker

```bash
# Install dependencies (one-time)
pip3 install -r requirements.txt

# Run
python3 worker.py
```

Requires a `.env` file in the project root:
```
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_KEY=your-service-role-key
```

Use the **`service_role`** key (not the anon key) — it bypasses RLS so the worker can read all users and write to `daily_menus`.

The worker:
1. Hits the Brown Dining API
2. Parses ~430 recipe items for today
3. Deletes stale `daily_menus` rows from previous days
4. Upserts today's rows in batches of 400
5. Loads all favorites and prints a match log

---

## Xcode Guide

### First-time setup

1. Open `BearBitesApp/Bear Bites.xcodeproj` in Xcode
2. In the top toolbar, click the destination selector (it says "Any iOS Device" by default) and pick a **simulator** like "iPhone 17 Pro"
3. Hit **Cmd+R** or the ▶ play button to build and run

### Running on a real device

1. Connect your iPhone with a **data cable** (not a charge-only cable — if the "Trust This Computer?" prompt never appears, the cable is charge-only)
2. Unlock your phone and tap **Trust** when prompted
3. Go to **Settings → Privacy & Security → Developer Mode** and enable it
4. Your device will appear in the destination selector — select it and hit ▶

### The preview canvas vs running the app

The **preview canvas** (right panel in Xcode) is a static sandbox. It renders the UI but **cannot make network calls**. Hearts won't fire, menus won't load. Always use **Cmd+R** to run in the full simulator or on a device for any feature that touches Supabase.

### Clean build

If the app seems to be running old code after changes, do:
**Product → Clean Build Folder** (Cmd+Shift+K), then **Cmd+R**.

---

## Agent Notes: Supabase Quick Reference

- Anonymous auth works but must be explicitly enabled in Supabase Dashboard → Authentication → Providers → Anonymous. Wait ~30 seconds after saving before testing — settings take time to propagate. Verify it's live with:
  ```bash
  curl -X POST "https://urfgilgpmacqslxfnrtz.supabase.co/auth/v1/signup" \
    -H "apikey: YOUR_ANON_KEY" \
    -H "Content-Type: application/json" \
    -d '{}'
  ```
  If it returns an `access_token`, anonymous auth is live. If it returns an error, the setting hasn't propagated yet.

- Always upsert into `users` before inserting into `favorites` due to the FK constraint (`favorites.user_id → users.id`). If you skip this, every favorites insert will fail with `violates foreign key constraint`.

- RLS is currently **disabled** on `users` and `favorites`. Re-enable it when real anonymous auth is wired up — the policies are already written in `supabase_schema.sql` and just need to be switched back on.

- `daily_menus` has RLS **enabled** with a public read policy. The worker writes to it using the `service_role` key which bypasses RLS entirely. The iOS app reads it with the anon key.

- The `service_role` key is in `.env` (backend only). The `anon` key is in `SupabaseManager.swift` (iOS app). Never swap these.

---

## Agent Notes: Editing Files

This project uses **Xcode 16's automatic filesystem sync** (`PBXFileSystemSynchronizedRootGroup`). This means:

**Any `.swift` file placed inside `BearBitesApp/Bear Bites/` on disk is automatically compiled** — you do not need to manually add files to `project.pbxproj`. There is no `AddFavoriteView` entry in the project file; Xcode picks it up from the directory.

### What this means for an AI agent

- Edit files directly at their path on disk (e.g. `BearBitesApp/Bear Bites/MenuBrowsingView.swift`). Xcode will detect the change and recompile on the next build.
- To create a new Swift view, write it to the correct folder. Xcode will include it automatically.
- **Do not** try to patch `project.pbxproj` to register files — it is not needed.
- After editing, the user must hit **Cmd+R** in Xcode to rebuild. If changes don't seem to take effect, suggest **Cmd+Shift+K** (Clean) then **Cmd+R**.

### Known gotchas

- **Smart quotes and curly apostrophes** (`"`, `"`, `'`, `…`) inside Swift string literals cause `Consecutive statements on a line must be separated by ';'` compile errors. Always use straight ASCII characters in Swift files.
- The **Supabase Swift SDK warns** about `emitLocalSessionAsInitialSession` on launch unless `autoRefreshToken: false` is set in `SupabaseClientOptions`. This is set in `SupabaseManager.swift`.
- **`DeviceID`** is defined in `SupabaseManager.swift` and shared across all views. Do not redefine it locally in individual views.
- **Foreign key constraint**: inserting into `favorites` requires a matching row in `users` first. `AddFavoriteView` and `MenuBrowsingView` both call `registerDevice()` which upserts into `users` before any favorites insert.

---

## Remaining Roadmap

### Next: Favorites List View
A screen showing everything the user has favorited, with a delete button. Query `favorites` filtered by `DeviceID.current`.

### After that: Real Auth
Replace `DeviceID` (UserDefaults UUID) with `supabase.auth.signInAnonymously()`. Re-enable RLS on `users` and `favorites`. The anonymous auth approach was designed for this from the start — the `users.id` primary key equals the Supabase Auth UUID, so the switch is clean.

### Optional account sign-in (Google / Sign in with Apple)
Give users the option to link their anonymous device account to a Google or Apple ID so their favorites sync across devices. Device-local favorites remain the default — sign-in is never required. When a user signs in, migrate their existing device favorites to the authenticated account.

### Recommendations (suggested favorites)
Show a curated "Start here" section at the top of the Discover tab with crowd-sourced popular dishes — e.g. Yakisoba Noodle, Ivy Room Big Burger, Egg Fried Rice, Bulgogi Chicken, Dry Noodle, Friday V-Dub Lunch Fried Chicken, Cheesecake. New users can set up meaningful favorites in seconds without needing to know exact menu item names. Tapping a suggestion hearts it just like browsing the catalog normally.

### Then: Push Notifications
1. Request APNs permission on app launch
2. Upload the device token to `users.apn_token`
3. In `worker.py`, replace the `print()` match log with real APNs HTTP/2 calls using the `.p8` auth key

### Polish
- Search across today's menu
- Allergen filter
- Hall and meal period filter
- Home screen widget
- TestFlight → App Store

---

## Dining Hall Reference

| `locationId` | Display Name | Nickname |
|---|---|---|
| `SHRP` | Sharpe Refectory | The Ratty |
| `VW` | Verney-Woolley | V-Dub |
| `AC` | Andrews Commons | Andrews |
| `JOS` | Josiah's | Jo's |
| `BR` | Blue Room | Blue Room |
| `IVY` | Ivy Room | Ivy Room |
| `SOE` | School of Engineering | SEANERD Cafe |

---

## Brown Dining API Reference

**Endpoint:** `GET https://esb-level1.brown.edu/services/oit/sys/brown-dining/v1/menus`

Returns a JSON array of 7 location objects covering a full week of menus.

```
Array (7 locations)
└── Location Object
    ├── name          : String   — e.g. "Sharpe Refectory"
    ├── locationId    : String   — e.g. "SHRP"
    └── meals         : Object   — keys are "YYYY-MM-DD" date strings
        └── "2026-03-03": Array
            └── Meal Period Object
                ├── meal  : String — "Breakfast" | "Lunch" | "Dinner"
                └── menu.stations: Array
                    └── Station Object
                        ├── name  : String — e.g. "Soups"
                        └── items : Array
                            └── Item Object
                                ├── item      : String — the food name (key field)
                                ├── itemType  : String — "recipe" | "ingredient"
                                ├── allergens : Array
                                └── icons     : Array — vegan, halal, etc.
```

Filter `itemType == "recipe"` to skip raw ingredients like "Butter" and "Salt".

---

## License

MIT
