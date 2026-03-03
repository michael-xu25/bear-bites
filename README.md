# BearBites 🐻
### Never Miss Your Favorite Meal at Brown

BearBites is an iOS app that sends Brown University students a push notification whenever a meal they love is being served at a campus dining hall — the Ratty, V-Dub, Andrews, and more. No more wandering to the Ratty only to find the pizza station is closed. Set your favorites once and let BearBites come to you.

---

## Table of Contents

1. [Tech Stack](#tech-stack)
2. [System Architecture](#system-architecture)
3. [Notification Flow](#notification-flow)
4. [JSON Schema Reference](#json-schema-reference)
5. [Python Parser Snippet](#python-parser-snippet)
6. [Development Roadmap](#development-roadmap)
7. [Dining Hall Reference](#dining-hall-reference)

---

## Tech Stack

| Layer | Technology |
|---|---|
| **iOS Frontend** | Swift / SwiftUI |
| **Push Notifications** | Apple Push Notification service (APNs) via Firebase Cloud Messaging (FCM) |
| **Backend Worker** | Python (Google Cloud Function or GitHub Actions Cron Job) |
| **Database** | Firebase Firestore |
| **Auth** | Firebase Authentication (Sign in with Apple) |
| **Source API** | Brown Dining Services — `https://esb-level1.brown.edu/services/oit/sys/brown-dining/v1/menus` |

---

## System Architecture

```
┌───────────────────────────────────────────────────────────────────┐
│                        BACKEND WORKER                             │
│          (Python Cloud Function, runs every 60 minutes)           │
│                                                                   │
│  1. Fetch 2.5 MB JSON from Brown Dining API  ──────────────────► │
│  2. Parse schema (location → date → meal → station → items)       │
│  3. Flatten into normalized menu records                          │
│  4. Upsert into Firestore  ──────────────────────────────────────►│
└───────────────────────────────────────────────────────────────────┘
         │
         ▼
┌────────────────────┐        ┌──────────────────────────────┐
│     Firestore DB   │        │        iOS App (SwiftUI)     │
│                    │        │                              │
│  menus/            │◄──────►│  • Browse today's menus      │
│  users/            │        │  • Search & favorite recipes │
│    └─ favorites[]  │        │  • Receive push alerts       │
└────────────────────┘        └──────────────────────────────┘
         │
         ▼
┌───────────────────────────────────────────────────────────────────┐
│                    NOTIFICATION TRIGGER WORKER                    │
│          (runs once at ~7 AM daily, or on menu upsert)            │
│                                                                   │
│  1. Read today's flattened menu from Firestore                    │
│  2. Read all users' favorites lists from Firestore                │
│  3. Intersect: favorites ∩ today's menu items                     │
│  4. For each match → send FCM → APNs → user's iPhone             │
└───────────────────────────────────────────────────────────────────┘
```

### Why a Backend Middleman?

The raw Brown Dining API response is **~2.5 MB of JSON** covering all dining halls for a full week. If the iOS app fetched this directly:

- 1,000 concurrent users at lunchtime = **2.5 GB of Brown API traffic in one minute**
- Risk of rate-limiting or an accidental denial-of-service on Brown's infrastructure
- Slow app startup (parsing 2.5 MB on-device on every launch)
- Wasted battery and data on parsing data for halls the user doesn't care about

The backend worker fetches this payload **once per hour**, parses it, and writes only the relevant structured records into Firestore. The iOS app then reads a tiny, pre-parsed document — typically under 10 KB per dining hall per day.

---

## Notification Flow

```
7:00 AM daily trigger
        │
        ▼
Cloud Function: notification_worker.py
        │
        ├─► Query Firestore: menus/{today}/{hall}/lunch/items[]
        │         (flat list of recipe names being served today)
        │
        ├─► Query Firestore: users/ (all documents)
        │         Each user doc contains: { fcmToken, favorites: ["Honey Yogurt Greek Chicken", ...] }
        │
        ├─► For each user:
        │       matches = set(user.favorites) ∩ set(today_menu_items)
        │       if matches:
        │           send FCM message to user.fcmToken
        │               title: "Your favorites are at the Ratty today!"
        │               body:  "Honey Yogurt Greek Chicken · Taco Filling Beef"
        │
        └─► Log delivery receipts to Firestore
```

**APNs delivery path:** Python backend → Firebase Cloud Messaging → Apple Push Notification service → user's iPhone

Users can configure:
- Which dining halls to watch
- Which meal periods to alert on (Breakfast / Lunch / Dinner)
- Alert timing (e.g., 30 min before service opens)

---

## JSON Schema Reference

The source API returns a **JSON array of 7 location objects**. Here is the annotated hierarchy:

```
Array (7 locations)
└── Location Object
    ├── name          : String   — Display name, e.g. "Sharpe Refectory"
    ├── locationId    : String   — Short ID, e.g. "SHRP"
    ├── locationAddress: String  — Street address
    └── meals         : Object   — Keys are ISO date strings ("YYYY-MM-DD")
        └── "2026-03-03": Array  — List of meal periods for that date
            └── Meal Period Object
                ├── name  : String — Long name, e.g. "Sharpe Spring Lunch"
                ├── meal  : String — Period type: "Breakfast" | "Lunch" | "Dinner"
                └── menu  : Object
                    ├── date   : String  — "YYYY-MM-DD"
                    ├── hours  : Object
                    │   ├── start : String — ISO 8601 timestamp with TZ offset
                    │   └── end   : String — ISO 8601 timestamp with TZ offset
                    └── stations: Array   — List of food stations
                        └── Station Object
                            ├── stationId : Integer — Numeric station ID
                            ├── name      : String  — e.g. "Soups", "Grill", "Pizza"
                            └── items     : Array   — List of food items
                                └── Item Object
                                    ├── itemId    : Integer — Unique recipe/ingredient ID
                                    ├── item      : String  — ★ The food name (key field)
                                    ├── itemType  : String  — "recipe" | "ingredient"
                                    ├── allergens : Array   — e.g. ["DAIRY", "WHEAT/GLUTEN"]
                                    ├── icons     : Array   — Dietary icons (vegan, halal, etc.)
                                    └── description: String — Optional description
```

**Key insight:** The food name you want to match against user favorites is always `item` inside each element of `station.items`. Filter by `itemType == "recipe"` to ignore raw ingredients and focus on named dishes.

### Dining Hall Location IDs

| `locationId` | Display Name | Nickname |
|---|---|---|
| `SHRP` | Sharpe Refectory | The Ratty |
| `VW` | Verney-Woolley | V-Dub |
| `AC` | Andrews Commons | Andrews |
| `JOS` | Josiah's | Jo's |
| `BR` | Blue Room | Blue Room |
| `IVY` | Ivy Room | Ivy Room |
| `SOE` | School of Engineering | SEANERD Café |

---

## Python Parser Snippet

This snippet demonstrates how to fetch the API, parse the schema, and print the **Lunch menu for Sharpe Refectory** for the first available date. All key lookups are guarded against missing data.

```python
import json
import urllib.request
from collections import defaultdict

API_URL = "https://esb-level1.brown.edu/services/oit/sys/brown-dining/v1/menus"


def fetch_menus(url: str = API_URL) -> list:
    """Fetch and decode the Brown Dining JSON payload."""
    with urllib.request.urlopen(url) as response:
        return json.loads(response.read().decode("utf-8"))


def get_meal_items(
    menus: list,
    location_id: str,
    date: str | None = None,
    meal_period: str = "Lunch",
    recipes_only: bool = True,
) -> dict[str, list[str]]:
    """
    Parse the menus payload and return a dict of {station_name: [item_names]}
    for the given location, date, and meal period.

    Args:
        menus:       The top-level list returned by the API.
        location_id: e.g. "SHRP" for Sharpe Refectory.
        date:        "YYYY-MM-DD". Defaults to the first available date.
        meal_period: "Breakfast", "Lunch", or "Dinner".
        recipes_only: If True, skips raw ingredients and returns only named recipes.

    Returns:
        Ordered dict of {station_name: [food_item_name, ...]}
    """
    # Find the matching location
    location = next(
        (loc for loc in menus if loc.get("locationId") == location_id), None
    )
    if location is None:
        print(f"Location '{location_id}' not found.")
        return {}

    all_meals: dict = location.get("meals", {})
    if not all_meals:
        print(f"No meal data for location '{location_id}'.")
        return {}

    # Default to first available date if none specified
    if date is None:
        date = sorted(all_meals.keys())[0]

    day_meals: list = all_meals.get(date, [])
    if not day_meals:
        print(f"No meals found for {location_id} on {date}.")
        return {}

    # Find the matching meal period
    period = next(
        (m for m in day_meals if m.get("meal", "").lower() == meal_period.lower()),
        None,
    )
    if period is None:
        print(f"No '{meal_period}' period found for {location_id} on {date}.")
        return {}

    stations: list = period.get("menu", {}).get("stations", [])
    result: dict[str, list[str]] = defaultdict(list)

    for station in stations:
        station_name: str = station.get("name", "Unknown Station")
        for item in station.get("items", []):
            if recipes_only and item.get("itemType") != "recipe":
                continue
            food_name = item.get("item", "").strip()
            if food_name:
                result[station_name].append(food_name)

    return dict(result)


# ── Demo ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Fetching Brown Dining menus...")
    menus = fetch_menus()

    location_id = "SHRP"
    meal_period = "Lunch"

    # Use first available date
    sharpe = next(loc for loc in menus if loc.get("locationId") == location_id)
    first_date = sorted(sharpe.get("meals", {}).keys())[0]

    print(f"\n{'='*60}")
    print(f"  {sharpe['name']} — {meal_period} — {first_date}")
    print(f"{'='*60}")

    menu_by_station = get_meal_items(menus, location_id, first_date, meal_period)

    if not menu_by_station:
        print("  (No items found)")
    else:
        for station, items in menu_by_station.items():
            print(f"\n  [{station}]")
            for item in items:
                print(f"    • {item}")

    # ── Example: check if a user's favorites are being served ─────────────────
    print(f"\n{'='*60}")
    print("  Favorites Check")
    print(f"{'='*60}")

    user_favorites = {"Honey Yogurt Greek Chicken", "Taco Filling Beef", "Sushi"}
    all_items_today = {
        item
        for items in menu_by_station.values()
        for item in items
    }
    matches = user_favorites & all_items_today

    if matches:
        print(f"\n  ✓ Your favorites being served today at the Ratty ({meal_period}):")
        for match in sorted(matches):
            print(f"    → {match}")
    else:
        print("\n  ✗ None of your favorites are being served today.")
```

---

## Development Roadmap

### Phase 1 — Backend Parser & Database
- [ ] Set up a Firebase project (Firestore + FCM)
- [ ] Write `parser.py` — fetches the Brown API and flattens the schema into Firestore documents following the structure: `menus/{YYYY-MM-DD}/{locationId}/{mealPeriod}/items[]`
- [ ] Deploy `parser.py` as a Google Cloud Function with a Cloud Scheduler trigger (every 60 minutes)
- [ ] Write unit tests for the parser against a cached copy of the JSON payload
- [ ] Handle edge cases: missing `stations`, empty `items`, API downtime (exponential backoff + cached fallback)

### Phase 2 — Database Schema & User Model
- [ ] Design Firestore schema:
  - `menus/{date}/{locationId}/{mealPeriod}` → `{ items: [{itemId, name, station, allergens}], hours: {start, end} }`
  - `users/{uid}` → `{ fcmToken, favorites: [itemId], watchedHalls: [locationId], alertMeals: ["Lunch", "Dinner"] }`
- [ ] Write Firestore security rules (users can only read/write their own document)
- [ ] Set up Firebase Authentication with Sign in with Apple

### Phase 3 — SwiftUI Frontend
- [ ] Project setup: Xcode project, add Firebase iOS SDK via Swift Package Manager
- [ ] **Meals tab:** Browse today's menu by dining hall, collapsible by station
- [ ] **Search:** Full-text search across all items for the current day
- [ ] **Favorites:** Tap the ★ on any recipe to add it to your favorites list; sync to Firestore user doc
- [ ] **Settings:** Choose which halls to watch, which meal periods to alert on, alert lead time
- [ ] Request push notification permissions on first launch; upload APNs token to Firestore

### Phase 4 — Push Notification Integration
- [ ] Upload APNs `.p8` auth key to Firebase Console
- [ ] Write `notifier.py` Cloud Function — triggered once daily at 7 AM ET
  - Reads today's flattened menu from Firestore
  - Reads all user documents
  - Computes `user.favorites ∩ today_menu_items` per user
  - Sends batched FCM messages to matching users
- [ ] Deep-link push notification taps to the correct dining hall + meal period screen
- [ ] Add "quiet hours" logic (respect Do Not Disturb preferences stored per user)
- [ ] Track delivery and open rates in Firestore for analytics

### Phase 5 — Polish & Launch
- [ ] App icon and splash screen
- [ ] Allergen filter (hide items containing user-specified allergens)
- [ ] Widget extension — show today's favorites on the home screen
- [ ] TestFlight beta with Brown students
- [ ] App Store submission

---

## Contributing

Pull requests welcome. Please open an issue first to discuss any major changes.

## License

MIT
