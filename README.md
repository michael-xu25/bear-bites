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
7. [Scheduling (cron-job.org + GitHub Actions)](#scheduling)
8. [Xcode Guide](#xcode-guide)
9. [Agent Notes: Supabase Quick Reference](#agent-notes-supabase-quick-reference)
10. [Agent Notes: Editing Files](#agent-notes-editing-files)
11. [Remaining Roadmap](#remaining-roadmap)
12. [Dining Hall Reference](#dining-hall-reference)
13. [Brown Dining API Reference](#brown-dining-api-reference)

---

## Current State

### What is built and working

| Component | Status | Description |
|---|---|---|
| Supabase database | ✅ Done | 3 tables: `users`, `favorites`, `daily_menus` |
| `worker.py` | ✅ Done | Fetches Brown API, parses full week's menus (today + ~6 upcoming days), syncs to `daily_menus` with per-date replace, matches today's menu against favorites, sends APNs push notifications |
| GitHub Actions + cron-job.org | ✅ Done | Worker runs automatically: 2 AM ET for menu sync, and via cron-job.org at 7:25 AM / 10:55 AM / 4:55 PM ET for meal-time notifications |
| APNs push notifications | ✅ Done | End-to-end: iOS app registers device token → uploaded to `users.apn_token` → worker sends ES256-signed HTTP/2 pushes via `api.sandbox.push.apple.com` |
| `SupabaseManager.swift` | ✅ Done | Shared Supabase client + `DeviceID` + `withRetry()` helper for transient network errors |
| `MenuBrowsingView.swift` | ✅ Done | Fetches today's menu from Supabase, grouped by hall + meal period, heart-to-favorite with persistence |
| `FavoritesView.swift` | ✅ Done | List of all saved favorites, swipe-to-delete |
| `ItemCatalogView.swift` | ✅ Done | Searchable catalog of all menu items seen in the past 7 days — browse by hall, search, heart to favorite, or add custom items via sheet |
| `AddFavoriteView.swift` | ✅ Done | Manual type-a-name favoriting (accessible as sheet from Discover tab) |
| `ContentView.swift` | ✅ Done | TabView with 3 tabs: Menu, Favorites, Discover |
| App icon | ✅ Done | Bear eating pizza, centered, saved to `design/app-icon-final-1024x1024.png` |

### What is NOT yet built

- Supabase Auth (currently using a `DeviceID` UUID stored in `UserDefaults` — planned replacement with `supabase.auth.signInAnonymously()`)
- Search, allergen filters, settings
- Home screen widget
- TestFlight → App Store distribution

### Recently fixed / shipped

- **Reliable scheduling:** Replaced unreliable GitHub Actions cron triggers with cron-job.org, which POSTs to the GitHub `workflow_dispatch` API at exact meal times. The 2 AM menu sync stays on GitHub Actions (timing not critical). Meal-time notification runs use `FORCE_NOTIFY=true` to bypass the time-window check in the worker.
- **Network resilience:** `SupabaseManager` now configures the URLSession with `waitsForConnectivity = true` (absorbs brief cellular dropouts before they become errors) and provides `withRetry()` — up to 3 attempts with 1 s / 2 s / 3 s backoff on transient `NSURLErrorDomain` codes. All favorite save/remove calls use this.
- **Duplicate menu items:** The same dish appearing at multiple stations within a meal period now shows only once in the list (deduplicated by food name in `MenuBrowsingView.grouped`).
- **Heart persistence:** Hearts now save to Supabase, survive app relaunches, and can be un-tapped. Fixed FK violation bug (missing `registerDevice()` call) and re-keyed state to food item name instead of volatile daily_menus row UUIDs.
- **Push notifications end-to-end:** Real APNs notifications working. iOS app requests permission on launch, receives device token, uploads to `users.apn_token`. Worker signs ES256 JWT from `.p8` key and POSTs via HTTP/2 (`httpx`). `APNS_SANDBOX=true` (default) uses sandbox endpoint for Xcode dev builds; set `false` for App Store/TestFlight.
- **Timezone fix:** Worker uses `America/New_York` (not system/UTC) so the date always matches Brown campus time.
- **Timed notifications:** Worker detects the upcoming meal period and only dispatches notifications for it. The 2 AM sync run returns early without sending anything.
- **Discover catalog:** `daily_menus` keeps a rolling 7-day window. Discover tab shows a searchable, hall-grouped catalog of all unique items seen this week.

---

## Tech Stack

| Layer | Technology |
|---|---|
| iOS Frontend | Swift / SwiftUI (Xcode 16) |
| Push Notifications | Apple Push Notification service (APNs) — token-based auth (.p8), sandbox + production |
| Backend Worker | Python 3.12, runs via GitHub Actions (triggered by cron-job.org) |
| Database | Supabase (PostgreSQL) |
| Auth | DeviceID via UserDefaults (Supabase anonymous auth planned) |
| Scheduling | cron-job.org → GitHub Actions `workflow_dispatch` for meal times; GitHub Actions `schedule` for 2 AM sync |
| Source API | Brown Dining Services — `https://esb-level1.brown.edu/services/oit/sys/brown-dining/v1/menus` |

---

## Project Structure

```
BearBites/
├── worker.py                        # Python backend worker
├── requirements.txt                 # Python dependencies (requests, supabase, httpx[http2], PyJWT, cryptography)
├── .env                             # Local env vars (never committed)
├── supabase_schema.sql              # users + favorites table DDL
├── add_daily_menus_table.sql        # daily_menus table DDL
├── design/
│   └── app-icon-final-1024x1024.png # Master copy of the app icon
│
├── .github/
│   └── workflows/
│       └── daily_worker.yml         # GitHub Actions: 2 AM sync + workflow_dispatch for notifications
│
└── BearBitesApp/
    └── Bear Bites/
        ├── Bear_BitesApp.swift      # App entry point + AppDelegate (APNs registration)
        ├── ContentView.swift        # TabView root — Menu / Favorites / Discover
        ├── MenuBrowsingView.swift   # Browse today's menu, tap to favorite
        ├── FavoritesView.swift      # List saved favorites, swipe to delete
        ├── ItemCatalogView.swift    # Discover tab: 7-day catalog, search, heart, add custom
        ├── AddFavoriteView.swift    # Manual type-a-name favoriting (sheet in Discover)
        ├── Assets.xcassets/
        │   └── AppIcon.appiconset/  # 1024×1024 universal icon (Xcode generates all sizes)
        └── Services/
            └── SupabaseManager.swift  # Supabase client + DeviceID + withRetry()
```

---

## System Architecture

```
cron-job.org (7:25 AM / 10:55 AM / 4:55 PM ET)
        │  POST to GitHub workflow_dispatch API
        ▼
┌─────────────────────────────────────────────────┐
│           worker.py  (GitHub Actions)           │
│                                                 │
│  1. Fetch Brown Dining API (2.5 MB JSON)        │
│  2. Parse all dates (today + ~6 upcoming)        │
│  3. Sync → Supabase daily_menus (~14-day window) │
│  4. Load user favorites + APN tokens            │
│  5. Cross-reference favorites vs today's menu   │
│  6. Filter to upcoming meal period              │
│  7. Send APNs push notifications (HTTP/2)       │
└─────────────────────────────────────────────────┘
        │
        ▼
┌─────────────────────┐      ┌──────────────────────────────────┐
│   Supabase DB       │      │         iOS App (SwiftUI)        │
│                     │      │                                  │
│  users              │◄────►│  MenuBrowsingView  (Menu tab)    │
│  favorites          │      │  FavoritesView     (Favorites)   │
│  daily_menus        │      │  ItemCatalogView   (Discover)    │
└─────────────────────┘      └──────────────────────────────────┘
                                          │
                              ┌───────────▼──────────┐
                              │  APNs (Apple)        │
                              │  sandbox / production│
                              └──────────────────────┘
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
| `apn_token` | TEXT | Apple Push token — uploaded by `AppDelegate` on registration |

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
APNS_KEY_ID=XXXXXXXXXX
APNS_TEAM_ID=XXXXXXXXXX
APNS_BUNDLE_ID=your.bundle.id
APNS_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----
APNS_SANDBOX=true
```

Use the **`service_role`** key (not the anon key) — it bypasses RLS so the worker can read all users and write to `daily_menus`.

**Environment variable notes:**
- `APNS_SANDBOX=true` (default) → `api.sandbox.push.apple.com` — use for Xcode dev builds
- `APNS_SANDBOX=false` → `api.push.apple.com` — use for App Store / TestFlight
- `FORCE_NOTIFY=true` → skip the meal-time window check and send notifications for the nearest meal period (set automatically by cron-job.org triggers)
- `APNS_PRIVATE_KEY` in `.env`: use literal `\n` between lines (single-line value). GitHub Actions secrets use real newlines — both are handled.

---

## Scheduling

The worker runs on two different triggers:

### 1. Daily menu sync — GitHub Actions `schedule`
Cron: `0 7 * * *` UTC = **2:00 AM ET**. Fetches and stores today's menu. No notifications sent (no meal period is upcoming at 2 AM). This run is fine if GitHub delays it by up to an hour.

### 2. Meal-time notifications — cron-job.org → `workflow_dispatch`
Three jobs on [cron-job.org](https://cron-job.org), each POSTing to the GitHub API at exact times (timezone: `America/New_York`):

| Meal | crontab | ET time |
|---|---|---|
| Breakfast | `25 7 * * *` | 7:25 AM |
| Lunch | `55 10 * * *` | 10:55 AM |
| Dinner | `55 16 * * *` | 4:55 PM |

Each job POSTs:
```
POST https://api.github.com/repos/michael-xu25/bear-bites/actions/workflows/daily_worker.yml/dispatches
Authorization: Bearer <github_pat_with_workflow_scope>
Accept: application/vnd.github.v3+json
Content-Type: application/json

{"ref": "main", "inputs": {"force_notify": "true"}}
```

**When cron-job.org calls, `FORCE_NOTIFY=true` is passed to the worker**, which bypasses the time-window check and dispatches notifications for the nearest meal period. This is necessary because GitHub job startup takes ~30 seconds and could push the run outside the normal ±20-minute window.

**The cron-job.org setup never needs to change** unless you rename the workflow file or move the repo. Code changes to `worker.py` are picked up automatically on the next run.

### Troubleshooting: scheduled workflow or cron-job failed

| Symptom | Likely cause | Mitigation (in repo) |
|--------|----------------|----------------------|
| Job fails only on `schedule` (2 AM), not on manual **Run workflow** | `FORCE_NOTIFY` was bound to `github.event.inputs.force_notify` — **inputs are null** when the trigger is `schedule`, which can break env evaluation. | Workflow sets `FORCE_NOTIFY` only when `github.event_name == 'workflow_dispatch'`, else `'false'`. |
| Worker exits before syncing `daily_menus` | Old logic returned early when **today** had no rows in the API (week rollover / glitch), **before** `sync_daily_menu` ran. | Worker always runs `sync_daily_menu` when the API returns any recipe rows; notifications are skipped only if today is empty. |
| Intermittent failure with no code change | Brown API timeout or 5xx on a ~2.5 MB response. | `fetch_menus()` retries with backoff and a longer read timeout (120 s). |
| GitHub: “scheduled workflows disabled” | Repo had **no commits for 60+ days** — GitHub auto-disables `schedule` until re-enabled in Actions tab. | Push any commit or re-enable workflows in **Actions** → **Daily Menu Sync** → … menu. |

---

## Xcode Guide

### First-time setup

1. Open `BearBitesApp/Bear Bites.xcodeproj` in Xcode
2. In the top toolbar, click the destination selector and pick your device or a simulator
3. Hit **Cmd+R** or the ▶ play button to build and run

### Running on a real device (required for push notifications)

Push notifications do not work in the simulator. For APNs:

1. Connect your iPhone with a **data cable** (not a charge-only cable — if the "Trust This Computer?" prompt never appears, the cable is charge-only)
2. Unlock your phone and tap **Trust** when prompted
3. Go to **Settings → Privacy & Security → Developer Mode** and enable it
4. In Xcode → Signing & Capabilities, select your Apple Developer team
5. Your device will appear in the destination selector — select it and hit ▶

### The preview canvas vs running the app

The **preview canvas** (right panel in Xcode) is a static sandbox. It renders the UI but **cannot make network calls**. Hearts won't fire, menus won't load. Always use **Cmd+R** for any feature that touches Supabase.

### Clean build

If the app seems to be running old code after changes:
**Product → Clean Build Folder** (Cmd+Shift+K), then **Cmd+R**.

---

## Agent Notes: Supabase Quick Reference

- Always upsert into `users` before inserting into `favorites` due to the FK constraint (`favorites.user_id → users.id`). If you skip this, every favorites insert will fail with `violates foreign key constraint`. Both `AddFavoriteView` and `MenuBrowsingView` call `registerDevice()` before any favorites write.

- RLS is currently **disabled** on `users` and `favorites`. Re-enable it when real anonymous auth is wired up — the policies are already written in `supabase_schema.sql`.

- `daily_menus` has RLS **enabled** with a public read policy. The worker writes to it using the `service_role` key which bypasses RLS entirely. The iOS app reads it with the anon key.

- The `service_role` key is in `.env` / GitHub Actions secrets (backend only). The `anon` key is hardcoded in `SupabaseManager.swift` (iOS app). Never swap these.

- The Supabase project URL is `https://urfgilgpmacqslxfnrtz.supabase.co`.

---

## Agent Notes: Editing Files

This project uses **Xcode 16's automatic filesystem sync** (`PBXFileSystemSynchronizedRootGroup`). This means:

**Any `.swift` file placed inside `BearBitesApp/Bear Bites/` on disk is automatically compiled** — you do not need to manually add files to `project.pbxproj`.

### What this means for an AI agent

- Edit files directly at their path on disk. Xcode will detect the change and recompile on the next build.
- To create a new Swift view, write it to the correct folder. Xcode will include it automatically.
- **Do not** try to patch `project.pbxproj` to register files — it is not needed.
- After editing, the user must hit **Cmd+R** in Xcode to rebuild. If changes don't seem to take effect, suggest **Cmd+Shift+K** (Clean) then **Cmd+R**.

### Known gotchas

- **Smart quotes and curly apostrophes** (`"`, `"`, `'`, `…`) inside Swift string literals cause `Consecutive statements on a line must be separated by ';'` compile errors. Always use straight ASCII characters in Swift files.
- **`DeviceID`** is defined in `SupabaseManager.swift` and shared across all views. Do not redefine it locally in individual views.
- **Foreign key constraint**: inserting into `favorites` requires a matching row in `users` first.
- **`SupabaseManager.withRetry()`**: wrap all Supabase write calls (save/delete favorite) in this helper. It retries up to 3× with exponential backoff on transient `NSURLErrorDomain` codes (`-1005`, `-1001`, `-1004`, `-1009`, `-1020`). Non-network errors propagate immediately.
- **URLSession config**: `SupabaseManager` passes a custom `URLSession` to the Supabase client with `waitsForConnectivity = true` and a 60 s resource timeout. This silently absorbs brief cellular dropouts before they ever become errors.
- The **Supabase Swift SDK** emits a startup warning unless `autoRefreshToken: false` and `emitLocalSessionAsInitialSession: true` are both set in `SupabaseClientOptions.AuthOptions`. Both are set in `SupabaseManager.swift`.

---

## Remaining Roadmap

### ~~Sync the full week's menu~~ ✅ Done

The worker now parses and stores **all date keys** from the API response (today + ~6 upcoming days) instead of only today. `parse_week_menus()` iterates every date key in the API payload; `sync_daily_menu()` does a **per-date DELETE + re-INSERT** on every sync run.

**Why per-date DELETE + re-INSERT (not upsert):**
Brown Dining occasionally swaps dishes in or out of a future date's menu between now and when that date arrives. A simple `ON CONFLICT DO NOTHING` would leave stale rows from earlier runs. By deleting all rows for a date before re-inserting, the DB always reflects the latest version of what the API says is planned.

**The resulting ~14-day Discover window:**
- **Past 7 days** — kept by the prune step (`DELETE WHERE date < 7 days ago`)
- **Today + ~6 upcoming days** — written fresh on every run from the API

The `daily_menus` table now gives the Discover tab a rolling window of roughly two weeks: recent history so users can find dishes they just saw, plus the upcoming week so users can pre-favorite something before it arrives.

**Notifications are unaffected:** `main()` splits the parsed results — all dates go to `sync_daily_menu`, but only today's entries are passed to `build_menu_index` and the notification matching steps.

### Optional account sign-in (Google / Sign in with Apple)
Give users the option to link their anonymous device account to a Google or Apple ID so their favorites sync across devices. Device-local favorites remain the default — sign-in is never required.

### Recommendations (suggested favorites)
Show a curated "Start here" section at the top of the Discover tab with crowd-sourced popular dishes — e.g. Yakisoba Noodle, Ivy Room Big Burger, Egg Fried Rice, Bulgogi Chicken, Dry Noodle, Friday V-Dub Lunch Fried Chicken, Cheesecake. New users can set up meaningful favorites in seconds without needing to know exact menu item names.

### Polish
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

Returns a JSON array of 7 location objects covering a full **week** of menus (today + upcoming days).

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

The `meals` object contains keys for multiple dates. The worker reads **all available date keys** via `parse_week_menus()` and syncs every date into `daily_menus` on each run.

---

## License

MIT
