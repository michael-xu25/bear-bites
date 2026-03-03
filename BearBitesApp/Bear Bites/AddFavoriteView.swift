import SwiftUI
import Supabase

// ---------------------------------------------------------------------------
// Data model — mirrors the `favorites` table columns
// ---------------------------------------------------------------------------

private struct FavoriteInsert: Encodable {
    let user_id: UUID
    let food_item: String
    let dining_hall_id: String?  // nil = alert from any dining hall
}

// ---------------------------------------------------------------------------
// View
// ---------------------------------------------------------------------------

struct AddFavoriteView: View {

    // MARK: — State

    @State private var foodName: String = ""
    @State private var isLoading: Bool = false
    @State private var successMessage: String? = nil
    @State private var errorMessage: String? = nil

    // Hardcoded dummy user ID for testing.
    // Replace this with `SupabaseManager.client.auth.currentUser?.id` once
    // anonymous authentication is set up (Phase 2 of the roadmap).
    //
    // IMPORTANT: For this insert to succeed, Row Level Security on the
    // `favorites` table must be temporarily DISABLED in the Supabase dashboard
    // while using a hardcoded UUID that doesn't belong to a real auth session.
    // Re-enable RLS before shipping.
    private let dummyUserID = UUID(uuidString: "00000000-0000-0000-0000-000000000001")!

    // MARK: — Body

    var body: some View {
        NavigationStack {
            Form {
                // ── Input ─────────────────────────────────────────────────
                Section(header: Text("What do you want alerts for?")) {
                    TextField("e.g. Spicy With, Honey Yogurt Greek Chicken", text: $foodName)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.words)
                }

                // ── Action ────────────────────────────────────────────────
                Section {
                    Button(action: saveFavorite) {
                        HStack {
                            Spacer()
                            if isLoading {
                                ProgressView()
                                    .padding(.trailing, 8)
                            }
                            Text(isLoading ? "Saving…" : "Save Favorite")
                                .fontWeight(.semibold)
                            Spacer()
                        }
                    }
                    .disabled(foodName.trimmingCharacters(in: .whitespaces).isEmpty || isLoading)
                }

                // ── Feedback ──────────────────────────────────────────────
                if let success = successMessage {
                    Section {
                        Label(success, systemImage: "checkmark.circle.fill")
                            .foregroundStyle(.green)
                    }
                }

                if let error = errorMessage {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle.fill")
                            .foregroundStyle(.red)
                    }
                }
            }
            .navigationTitle("Add Favorite")
        }
    }

    // MARK: — Actions

    private func saveFavorite() {
        let trimmed = foodName.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }

        isLoading = true
        successMessage = nil
        errorMessage = nil

        Task {
            do {
                let row = FavoriteInsert(
                    user_id: dummyUserID,
                    food_item: trimmed,
                    dining_hall_id: nil   // nil = match this item at any dining hall
                )

                try await SupabaseManager.client
                    .from("favorites")
                    .insert(row)
                    .execute()

                await MainActor.run {
                    successMessage = ""\(trimmed)" saved! You'll be notified when it's served."
                    foodName = ""
                    isLoading = false
                }

            } catch {
                await MainActor.run {
                    errorMessage = error.localizedDescription
                    isLoading = false
                }
            }
        }
    }
}

// ---------------------------------------------------------------------------
// Preview
// ---------------------------------------------------------------------------

#Preview {
    AddFavoriteView()
}
