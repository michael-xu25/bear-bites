import SwiftUI
import Supabase

// ---------------------------------------------------------------------------
// Encodable structs — mirror the database table columns exactly
// ---------------------------------------------------------------------------

private struct UserUpsert: Encodable {
    let id: UUID
}

private struct FavoriteInsert: Encodable {
    let user_id: UUID
    let food_item: String
    let dining_hall_id: String?
}

// ---------------------------------------------------------------------------
// View
// ---------------------------------------------------------------------------

struct AddFavoriteView: View {

    // MARK: - State

    @State private var foodName: String = ""
    @State private var isLoading: Bool = false
    @State private var successMessage: String? = nil
    @State private var errorMessage: String? = nil
    @State private var isReady: Bool = false

    // MARK: - Body

    var body: some View {
        NavigationStack {
            Form {
                Section(header: Text("What do you want alerts for?")) {
                    TextField("e.g. Spicy With, Honey Yogurt Greek Chicken", text: $foodName)
                        .autocorrectionDisabled()
                        .textInputAutocapitalization(.words)
                }

                Section {
                    Button(action: saveFavorite) {
                        HStack {
                            Spacer()
                            if isLoading {
                                ProgressView()
                                    .padding(.trailing, 8)
                            }
                            Text(isLoading ? "Saving..." : "Save Favorite")
                                .fontWeight(.semibold)
                            Spacer()
                        }
                    }
                    .disabled(
                        foodName.trimmingCharacters(in: .whitespaces).isEmpty
                        || isLoading
                        || !isReady
                    )
                }

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
            .task {
                await registerDevice()
            }
        }
    }

    // MARK: - Device registration

    /// Upserts this device's UUID into public.users so the foreign key
    /// from favorites.user_id is always satisfied.
    /// Requires RLS to be DISABLED on both the users and favorites tables
    /// in the Supabase dashboard while using this dev-phase auth approach.
    private func registerDevice() async {
        do {
            try await SupabaseManager.client
                .from("users")
                .upsert(UserUpsert(id: DeviceID.current))
                .execute()
            isReady = true
        } catch {
            errorMessage = "Device registration failed: \(error.localizedDescription)"
        }
    }

    // MARK: - Save

    private func saveFavorite() {
        let trimmed = foodName.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }

        isLoading = true
        successMessage = nil
        errorMessage = nil

        Task {
            do {
                let row = FavoriteInsert(
                    user_id: DeviceID.current,
                    food_item: trimmed,
                    dining_hall_id: nil
                )

                try await SupabaseManager.client
                    .from("favorites")
                    .insert(row)
                    .execute()

                await MainActor.run {
                    successMessage = "\"\(trimmed)\" saved! You will be notified when it is served."
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
