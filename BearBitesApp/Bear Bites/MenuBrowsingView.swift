import SwiftUI
import Supabase

// ---------------------------------------------------------------------------
// MARK: - Data Model
// ---------------------------------------------------------------------------

/// One row from the daily_menus table.
/// Property names match the Supabase column names exactly so the Supabase
/// Swift SDK can decode the JSON response without a custom CodingKeys mapping.
struct DailyMenuItem: Codable, Identifiable, Hashable {
    let id: UUID
    let dining_hall_id: String    // e.g. "SHRP"
    let dining_hall_name: String  // e.g. "Sharpe Refectory"
    let meal_period: String       // "Breakfast" | "Lunch" | "Dinner"
    let station: String           // e.g. "Soups", "Grill"
    let food_item: String         // e.g. "Honey Yogurt Greek Chicken"
}

/// Row inserted into favorites when the user taps the heart.
private struct FavoriteInsert: Encodable {
    let user_id: UUID
    let food_item: String
    let dining_hall_id: String
}

// ---------------------------------------------------------------------------
// MARK: - Helpers
// ---------------------------------------------------------------------------

/// Canonical sort order for meal periods so Breakfast always comes before
/// Lunch, and Lunch before Dinner, regardless of API response ordering.
private let mealPeriodOrder = ["Breakfast", "Lunch", "Dinner"]

/// Returns today's date as "YYYY-MM-DD" — matches the date column in Supabase.
private var todayString: String {
    let f = DateFormatter()
    f.dateFormat = "yyyy-MM-dd"
    return f.string(from: Date())
}

// ---------------------------------------------------------------------------
// MARK: - MenuBrowsingView
// ---------------------------------------------------------------------------

struct MenuBrowsingView: View {

    // MARK: State

    @State private var items: [DailyMenuItem] = []
    @State private var isLoading = true
    @State private var errorMessage: String? = nil

    /// IDs of daily_menus rows the user has favorited this session.
    /// Used to toggle the heart icon optimistically without a round-trip.
    @State private var favoritedIDs: Set<UUID> = []

    // MARK: Computed grouping

    /// Groups items into a sorted array of dining halls, each containing
    /// a sorted array of meal periods with their respective food items.
    ///
    ///   grouped
    ///   └── (hallName, hallID, periods)
    ///           └── (mealPeriod, items sorted A→Z)
    private var grouped: [(hall: String, hallID: String, periods: [(period: String, items: [DailyMenuItem])])] {
        let byHall = Dictionary(grouping: items, by: \.dining_hall_id)

        return byHall
            .map { hallID, hallItems in
                let hallName = hallItems.first?.dining_hall_name ?? hallID

                let byPeriod = Dictionary(grouping: hallItems, by: \.meal_period)
                let sortedPeriods = byPeriod
                    .map { period, periodItems in
                        (
                            period: period,
                            items: periodItems.sorted { $0.food_item < $1.food_item }
                        )
                    }
                    .sorted { a, b in
                        // Sort by the canonical meal-period order defined above.
                        let ai = mealPeriodOrder.firstIndex(of: a.period) ?? 99
                        let bi = mealPeriodOrder.firstIndex(of: b.period) ?? 99
                        return ai < bi
                    }

                return (hall: hallName, hallID: hallID, periods: sortedPeriods)
            }
            .sorted { $0.hall < $1.hall }  // Halls A → Z
    }

    // MARK: Body

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    ProgressView("Loading today's menu...")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)

                } else if let error = errorMessage {
                    ContentUnavailableView(
                        "Could not load menu",
                        systemImage: "wifi.slash",
                        description: Text(error)
                    )

                } else if items.isEmpty {
                    ContentUnavailableView(
                        "No menu today",
                        systemImage: "fork.knife",
                        description: Text("Check back later or re-run the worker.")
                    )

                } else {
                    menuList
                }
            }
            .navigationTitle("Today's Menu")
            .task {
                await fetchMenu()
            }
        }
    }

    // MARK: List

    private var menuList: some View {
        List {
            ForEach(grouped, id: \.hallID) { group in
                Section {
                    ForEach(group.periods, id: \.period) { periodGroup in

                        // Meal-period sub-header row (not tappable, just a label)
                        HStack {
                            Text(periodGroup.period)
                                .font(.subheadline)
                                .fontWeight(.semibold)
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text("\(periodGroup.items.count) items")
                                .font(.caption)
                                .foregroundStyle(.tertiary)
                        }
                        .listRowBackground(Color(.systemGroupedBackground))

                        // Food item rows for this meal period
                        ForEach(periodGroup.items) { item in
                            MenuItemRow(
                                item: item,
                                isFavorited: favoritedIDs.contains(item.id),
                                onFavorite: { await saveFavorite(item) }
                            )
                        }
                    }
                } header: {
                    Text(group.hall)
                        .font(.headline)
                        .textCase(nil)
                        .foregroundStyle(.primary)
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    // MARK: Data fetching

    private func fetchMenu() async {
        isLoading = true
        errorMessage = nil

        do {
            let response: [DailyMenuItem] = try await SupabaseManager.client
                .from("daily_menus")
                .select("id, dining_hall_id, dining_hall_name, meal_period, station, food_item")
                .eq("date", value: todayString)
                .execute()
                .value

            items = response
        } catch {
            errorMessage = error.localizedDescription
        }

        isLoading = false
    }

    // MARK: Favoriting

    private func saveFavorite(_ item: DailyMenuItem) async {
        // Optimistic UI update — fill the heart before the network call so
        // the response feels instant.
        favoritedIDs.insert(item.id)

        let row = FavoriteInsert(
            user_id: DeviceID.current,
            food_item: item.food_item,
            dining_hall_id: item.dining_hall_id
        )

        do {
            try await SupabaseManager.client
                .from("favorites")
                .insert(row)
                .execute()

            print("[BearBites] ♥ Favorited \"\(item.food_item)\" at \(item.dining_hall_id)")

        } catch {
            // Roll back the optimistic update so the UI stays accurate.
            favoritedIDs.remove(item.id)
            print("[BearBites] Failed to save favorite: \(error.localizedDescription)")
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - MenuItemRow
// ---------------------------------------------------------------------------

private struct MenuItemRow: View {
    let item: DailyMenuItem
    let isFavorited: Bool
    let onFavorite: () async -> Void

    var body: some View {
        HStack(spacing: 12) {
            // Food info
            VStack(alignment: .leading, spacing: 2) {
                Text(item.food_item)
                    .font(.body)
                Text(item.station)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Spacer()

            // Heart button — .plain style prevents the tap gesture from
            // propagating to the list row selection.
            Button {
                Task { await onFavorite() }
            } label: {
                Image(systemName: isFavorited ? "heart.fill" : "heart")
                    .foregroundStyle(isFavorited ? .red : .secondary)
                    .imageScale(.large)
                    .animation(.easeInOut(duration: 0.15), value: isFavorited)
            }
            .buttonStyle(.plain)
        }
        .padding(.vertical, 2)
    }
}

// ---------------------------------------------------------------------------
// MARK: - Preview
// ---------------------------------------------------------------------------

#Preview {
    MenuBrowsingView()
}
