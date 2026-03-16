import SwiftUI
import Supabase

// ---------------------------------------------------------------------------
// MARK: - Data Model
// ---------------------------------------------------------------------------

/// A unique (food_item, dining_hall) pair loaded from the 7-day daily_menus
/// rolling window. Used to let users browse and favorite items they have not
/// seen on today's menu yet.
struct CatalogItem: Decodable, Hashable {
    let food_item: String
    let dining_hall_id: String
    let dining_hall_name: String
}

/// Row read back from favorites to pre-populate the heart state.
private struct CatalogFavoriteRow: Decodable {
    let food_item: String
}

/// Row inserted into favorites when the user taps a heart in the catalog.
private struct CatalogFavoriteInsert: Encodable {
    let user_id: UUID
    let food_item: String
    let dining_hall_id: String
}

// ---------------------------------------------------------------------------
// MARK: - ItemCatalogView
// ---------------------------------------------------------------------------

struct ItemCatalogView: View {

    // MARK: State

    @State private var allItems: [CatalogItem] = []
    @State private var favoritedFoods: Set<String> = []
    @State private var isLoading = true
    @State private var errorMessage: String? = nil
    @State private var searchText = ""
    @State private var showAddCustomSheet = false

    // MARK: Computed

    /// Deduplicated (food_item, dining_hall_id) pairs from the raw DB rows.
    private var uniqueItems: [CatalogItem] {
        var seen = Set<CatalogItem>()
        return allItems.filter { seen.insert($0).inserted }
    }

    /// When search is active: flat filtered list sorted A-Z.
    private var searchResults: [CatalogItem] {
        guard !searchText.isEmpty else { return [] }
        return uniqueItems.filter {
            $0.food_item.localizedCaseInsensitiveContains(searchText)
        }.sorted { $0.food_item < $1.food_item }
    }

    /// Grouped by hall for the default (non-search) view.
    private var grouped: [(hall: String, hallID: String, items: [CatalogItem])] {
        let byHall = Dictionary(grouping: uniqueItems, by: \.dining_hall_id)
        return byHall
            .map { hallID, hallItems in
                let hallName = hallItems.first?.dining_hall_name ?? hallID
                let sorted = hallItems.sorted { $0.food_item < $1.food_item }
                return (hall: hallName, hallID: hallID, items: sorted)
            }
            .sorted { $0.hall < $1.hall }
    }

    // MARK: Body

    var body: some View {
        NavigationStack {
            Group {
                if isLoading {
                    ProgressView("Loading catalog...")
                        .frame(maxWidth: .infinity, maxHeight: .infinity)

                } else if let error = errorMessage {
                    ContentUnavailableView(
                        "Could not load catalog",
                        systemImage: "wifi.slash",
                        description: Text(error)
                    )

                } else if uniqueItems.isEmpty {
                    ContentUnavailableView(
                        "Catalog is empty",
                        systemImage: "fork.knife",
                        description: Text("The catalog fills up as the daily menu syncs. Check back tomorrow or use \"Add custom\" to save a favorite by name.")
                    )

                } else {
                    catalogList
                }
            }
            .navigationTitle("Discover")
            .searchable(text: $searchText, prompt: "Search all menu items")
            .toolbar {
                ToolbarItem(placement: .navigationBarTrailing) {
                    Button("Add custom") { showAddCustomSheet = true }
                }
            }
            .sheet(isPresented: $showAddCustomSheet) {
                AddFavoriteView()
            }
            .task {
                await loadCatalog()
            }
        }
    }

    // MARK: List

    @ViewBuilder
    private var catalogList: some View {
        List {
            if !uniqueItems.isEmpty && searchText.isEmpty {
                Section {
                    Text("The catalog grows daily as new menus are synced. Tap a heart to get notified whenever that item is served.")
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }

            if searchText.isEmpty {
                // Grouped view
                ForEach(grouped, id: \.hallID) { group in
                    Section(header:
                        Text(group.hall)
                            .font(.headline)
                            .textCase(nil)
                            .foregroundStyle(.primary)
                    ) {
                        ForEach(group.items, id: \.self) { item in
                            CatalogItemRow(
                                item: item,
                                isFavorited: favoritedFoods.contains(item.food_item),
                                onFavorite: { await toggleFavorite(item) }
                            )
                        }
                    }
                }
            } else {
                // Flat search results
                if searchResults.isEmpty {
                    ContentUnavailableView.search(text: searchText)
                } else {
                    ForEach(searchResults, id: \.self) { item in
                        CatalogItemRow(
                            item: item,
                            isFavorited: favoritedFoods.contains(item.food_item),
                            onFavorite: { await toggleFavorite(item) }
                        )
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    // MARK: Data loading

    private func loadCatalog() async {
        isLoading = true
        errorMessage = nil

        await withTaskGroup(of: Void.self) { group in
            group.addTask { await fetchCatalogItems() }
            group.addTask { await loadFavorites() }
        }

        isLoading = false
    }

    private func fetchCatalogItems() async {
        do {
            let rows: [CatalogItem] = try await SupabaseManager.client
                .from("daily_menus")
                .select("food_item, dining_hall_id, dining_hall_name")
                .order("food_item", ascending: true)
                .execute()
                .value

            allItems = rows
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func loadFavorites() async {
        do {
            let rows: [CatalogFavoriteRow] = try await SupabaseManager.client
                .from("favorites")
                .select("food_item")
                .eq("user_id", value: DeviceID.current.uuidString)
                .execute()
                .value

            favoritedFoods = Set(rows.map(\.food_item))
        } catch {
            print("[BearBites] Catalog: failed to load favorites: \(error.localizedDescription)")
        }
    }

    // MARK: Favoriting

    private func toggleFavorite(_ item: CatalogItem) async {
        if favoritedFoods.contains(item.food_item) {
            await removeFavorite(item)
        } else {
            await saveFavorite(item)
        }
    }

    private func saveFavorite(_ item: CatalogItem) async {
        favoritedFoods.insert(item.food_item)

        let row = CatalogFavoriteInsert(
            user_id: DeviceID.current,
            food_item: item.food_item,
            dining_hall_id: item.dining_hall_id
        )

        do {
            try await SupabaseManager.withRetry {
                try await SupabaseManager.client
                    .from("favorites")
                    .upsert(row, onConflict: "user_id,food_item,dining_hall_id")
                    .execute()
            }
            print("[BearBites] Catalog: favorited \"\(item.food_item)\"")
        } catch {
            favoritedFoods.remove(item.food_item)
            print("[BearBites] Catalog: failed to save favorite after retries: \(error.localizedDescription)")
        }
    }

    private func removeFavorite(_ item: CatalogItem) async {
        favoritedFoods.remove(item.food_item)

        do {
            try await SupabaseManager.withRetry {
                try await SupabaseManager.client
                    .from("favorites")
                    .delete()
                    .eq("user_id", value: DeviceID.current.uuidString)
                    .eq("food_item", value: item.food_item)
                    .eq("dining_hall_id", value: item.dining_hall_id)
                    .execute()
            }
            print("[BearBites] Catalog: unfavorited \"\(item.food_item)\"")
        } catch {
            favoritedFoods.insert(item.food_item)
            print("[BearBites] Catalog: failed to remove favorite after retries: \(error.localizedDescription)")
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - CatalogItemRow
// ---------------------------------------------------------------------------

private struct CatalogItemRow: View {
    let item: CatalogItem
    let isFavorited: Bool
    let onFavorite: () async -> Void

    var body: some View {
        HStack(spacing: 12) {
            Text(item.food_item)
                .font(.body)

            Spacer()

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
    ItemCatalogView()
}
