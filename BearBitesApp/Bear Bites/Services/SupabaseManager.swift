import Foundation
import Supabase

// ---------------------------------------------------------------------------
// Supabase client — shared across the entire app.
// Use the anon/public key here. Never use the service_role key in the iOS app.
// Values are in: Supabase Dashboard → Project Settings → API
// ---------------------------------------------------------------------------

enum SupabaseManager {
    static let client = SupabaseClient(
        supabaseURL: URL(string: "https://urfgilgpmacqslxfnrtz.supabase.co")!,
        supabaseKey: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVyZmdpbGdwbWFjcXNseGZucnR6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI1NjcyOTksImV4cCI6MjA4ODE0MzI5OX0.dnVNSvcfLsfobzos6IxI2yxQXC6FU7jscrprf1evHKk"
    )
}

// ---------------------------------------------------------------------------
// DeviceID — persistent anonymous device identity.
//
// Generates a UUID on first launch, stores it in UserDefaults, and reuses it
// on every subsequent launch. Used as the user_id for the users + favorites
// tables until real Supabase Auth is wired up in a later phase.
// ---------------------------------------------------------------------------

enum DeviceID {
    static var current: UUID {
        let key = "bear_bites_device_id"
        if let stored = UserDefaults.standard.string(forKey: key),
           let uuid = UUID(uuidString: stored) {
            return uuid
        }
        let fresh = UUID()
        UserDefaults.standard.set(fresh.uuidString, forKey: key)
        return fresh
    }
}
