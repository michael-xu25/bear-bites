import SwiftUI
import Supabase

// ---------------------------------------------------------------------------
// MARK: - Data Models
// ---------------------------------------------------------------------------

struct FavoriteItem: Identifiable, Decodable {
    let id: UUID
    let food_item: String
    let dining_hall_id: String?

    var hallDisplay: String {
        dining_hall_id ?? "Any hall"
    }
}

private struct UpcomingItem: Decodable, Hashable {
    let date: String
    let food_item: String
    let dining_hall_id: String
    let dining_hall_name: String
    let meal_period: String
}

// ---------------------------------------------------------------------------
// MARK: - FavoritesView
// ---------------------------------------------------------------------------

struct FavoritesView: View {

    @State private var favorites: [FavoriteItem] = []
    @State private var upcomingItems: [UpcomingItem] = []
    @State private var isLoading = true
    @State private var errorMessage: String? = nil

    private let mealOrder = ["Breakfast", "Lunch", "Dinner"]

    /// Upcoming occurrences grouped by day label, each group's meals sorted
    /// Breakfast → Lunch → Dinner, then A-Z within each meal.
    private var upcomingGrouped: [(label: String, items: [UpcomingItem])] {
        let grouped = Dictionary(grouping: upcomingItems, by: \.date)
        return grouped.keys.sorted().map { date in
            let sorted = (grouped[date] ?? []).sorted {
                let ai = mealOrder.firstIndex(of: $0.meal_period) ?? 99
                let bi = mealOrder.firstIndex(of: $1.meal_period) ?? 99
                if ai != bi { return ai < bi }
                return $0.food_item < $1.food_item
            }
            return (label: dayLabel(for: date), items: sorted)
        }
    }

    // MARK: Body

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    ProgressView("Loading favorites...")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)

                } else if let error = errorMessage {
                    ContentUnavailableView(
                        "Could not load favorites",
                        systemImage: "wifi.slash",
                        description: Text(error)
                    )

                } else if favorites.isEmpty {
                    ContentUnavailableView(
                        "No favorites yet",
                        systemImage: "heart",
                        description: Text("Heart items on the Menu tab to get notified when they're served.")
                    )

                } else {
                    favoritesList
                }
            }
            .navigationTitle("Favorites")
            .task {
                await loadData()
            }
        }
    }

    // MARK: List

    private var favoritesList: some View {
        List {
            // Up Next — upcoming occurrences of favorited dishes this week.
            if !upcomingGrouped.isEmpty {
                ForEach(upcomingGrouped, id: \.label) { group in
                    Section(header:
                        Text(group.label)
                            .font(.headline)
                            .textCase(nil)
                            .foregroundStyle(.primary)
                    ) {
                        ForEach(group.items, id: \.self) { item in
                            VStack(alignment: .leading, spacing: 2) {
                                Text(item.food_item)
                                    .font(.body)
                                Text("\(item.dining_hall_name) \u{00B7} \(item.meal_period)")
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            .padding(.vertical, 2)
                        }
                    }
                }
            }

            // All Favorites — permanent list with swipe-to-delete.
            Section("All Favorites") {
                ForEach(favorites) { item in
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.food_item)
                            .font(.body)
                        Text(item.hallDisplay)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 2)
                }
                .onDelete { indexSet in
                    Task { await deleteItems(at: indexSet) }
                }
            }
        }
        .listStyle(.insetGrouped)
        .toolbar {
            EditButton()
        }
    }

    // MARK: Data loading

    private func loadData() async {
        isLoading = true
        errorMessage = nil

        do {
            let rows: [FavoriteItem] = try await SupabaseManager.client
                .from("favorites")
                .select("id, food_item, dining_hall_id")
                .eq("user_id", value: DeviceID.current.uuidString)
                .order("food_item", ascending: true)
                .execute()
                .value

            favorites = rows
            isLoading = false

            // Fetch upcoming after the list is already visible.
            await loadUpcoming(for: rows)

        } catch is CancellationError {
            isLoading = false
        } catch {
            errorMessage = error.localizedDescription
            isLoading = false
        }
    }

    /// Queries daily_menus for upcoming occurrences of the given favorites,
    /// then filters client-side to respect each favorite's hall constraint.
    private func loadUpcoming(for favs: [FavoriteItem]) async {
        guard !favs.isEmpty else {
            upcomingItems = []
            return
        }

        let foodNames = Array(Set(favs.map(\.food_item)))

        let todayStr: String = {
            let f = DateFormatter()
            f.dateFormat = "yyyy-MM-dd"
            return f.string(from: Date())
        }()

        do {
            let rows: [UpcomingItem] = try await SupabaseManager.client
                .from("daily_menus")
                .select("date, food_item, dining_hall_id, dining_hall_name, meal_period")
                .gte("date", value: todayStr)
                .in("food_item", values: foodNames)
                .order("date", ascending: true)
                .limit(500)
                .execute()
                .value

            // Build a map of food_item → [hall constraint] where nil = any hall.
            var hallConstraints: [String: [String?]] = [:]
            for fav in favs {
                hallConstraints[fav.food_item, default: []].append(fav.dining_hall_id)
            }

            // Keep only occurrences matched by at least one of the user's favorites.
            upcomingItems = rows.filter { row in
                guard let constraints = hallConstraints[row.food_item] else { return false }
                return constraints.contains { $0 == nil || $0 == row.dining_hall_id }
            }

        } catch is CancellationError {
            return
        } catch {
            print("[BearBites] Upcoming load failed: \(error.localizedDescription)")
        }
    }

    // MARK: Delete

    private func deleteItems(at indexSet: IndexSet) async {
        let toDelete = indexSet.map { favorites[$0] }
        favorites.remove(atOffsets: indexSet)

        for item in toDelete {
            do {
                try await SupabaseManager.withRetry {
                    try await SupabaseManager.client
                        .from("favorites")
                        .delete()
                        .eq("id", value: item.id.uuidString)
                        .execute()
                }
            } catch {
                favorites.append(item)
                favorites.sort { $0.food_item < $1.food_item }
                print("[BearBites] Failed to delete favorite after retries: \(error.localizedDescription)")
            }
        }

        // Reload upcoming to reflect the updated favorites list.
        await loadUpcoming(for: favorites)
    }

    // MARK: Date label

    /// Converts "YYYY-MM-DD" to a human-readable day label:
    /// "Today", "Tomorrow", or a weekday name ("Wednesday").
    private func dayLabel(for dateString: String) -> String {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        guard let date = f.date(from: dateString) else { return dateString }
        let cal = Calendar.current
        if cal.isDateInToday(date) { return "Today" }
        if cal.isDateInTomorrow(date) { return "Tomorrow" }
        let df = DateFormatter()
        df.dateFormat = "EEEE"
        return df.string(from: date)
    }
}

// ---------------------------------------------------------------------------
// MARK: - Preview
// ---------------------------------------------------------------------------

#Preview {
    FavoritesView()
}
