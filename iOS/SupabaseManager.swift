import Foundation
import Supabase

/// Shared Supabase client for the entire app.
///
/// Replace the two placeholder strings below with your actual project values
/// from: Supabase Dashboard → Project Settings → API
///
///  - supabaseURL : "Project URL"          (e.g. https://xyzxyz.supabase.co)
///  - supabaseKey : "anon public" key      ← the PUBLISHABLE key, never the service_role key
///
/// Place this file anywhere in your Xcode project target (e.g. a "Services" group).
/// Because it is a `let` at file scope, the client is initialized once and shared
/// across the whole app via `SupabaseManager.client`.

enum SupabaseManager {
    static let client = SupabaseClient(
        supabaseURL: URL(string: "YOUR_SUPABASE_PROJECT_URL")!,
        supabaseKey: "YOUR_SUPABASE_ANON_PUBLIC_KEY"
    )
}
