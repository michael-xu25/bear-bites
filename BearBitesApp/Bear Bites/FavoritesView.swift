import SwiftUI
import Supabase

// ---------------------------------------------------------------------------
// MARK: - Data Model
// ---------------------------------------------------------------------------

struct FavoriteItem: Identifiable, Decodable {
    let id: UUID
    let food_item: String
    let dining_hall_id: String?

    var hallDisplay: String {
        dining_hall_id ?? "Any hall"
    }
}

// ---------------------------------------------------------------------------
// MARK: - FavoritesView
// ---------------------------------------------------------------------------

struct FavoritesView: View {

    @State private var favorites: [FavoriteItem] = []
    @State private var isLoading = true
    @State private var errorMessage: String? = nil

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
                await loadFavorites()
            }
        }
    }

    // MARK: List

    private var favoritesList: some View {
        List {
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
        .listStyle(.insetGrouped)
        .toolbar {
            EditButton()
        }
    }

    // MARK: Data

    private func loadFavorites() async {
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
        } catch {
            errorMessage = error.localizedDescription
        }

        isLoading = false
    }

    private func deleteItems(at indexSet: IndexSet) async {
        let toDelete = indexSet.map { favorites[$0] }

        // Optimistic removal.
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
                // Re-insert on failure so the UI stays accurate.
                favorites.append(item)
                favorites.sort { $0.food_item < $1.food_item }
                print("[BearBites] Failed to delete favorite after retries: \(error.localizedDescription)")
            }
        }
    }
}

// ---------------------------------------------------------------------------
// MARK: - Preview
// ---------------------------------------------------------------------------

#Preview {
    FavoritesView()
}
