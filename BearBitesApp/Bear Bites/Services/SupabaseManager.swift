import Foundation
import Supabase

// ---------------------------------------------------------------------------
// Supabase client — shared across the entire app.
// Use the anon/public key here. Never use the service_role key in the iOS app.
// Values are in: Supabase Dashboard → Project Settings → API
// ---------------------------------------------------------------------------

enum SupabaseManager {

    // Custom URLSession with mobile-resilient settings:
    //   waitsForConnectivity — don't fail on a brief signal dip; wait up to
    //   timeoutIntervalForResource seconds for the network to recover before
    //   attempting the request.  This eliminates most -1005 / -1001 errors
    //   caused by QUIC keepalive timeouts on cellular.
    private static let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.waitsForConnectivity = true
        config.timeoutIntervalForRequest  = 30   // per-request flight timeout
        config.timeoutIntervalForResource = 60   // includes waiting for network
        return URLSession(configuration: config)
    }()

    static let client = SupabaseClient(
        supabaseURL: URL(string: "https://urfgilgpmacqslxfnrtz.supabase.co")!,
        supabaseKey: "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InVyZmdpbGdwbWFjcXNseGZucnR6Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzI1NjcyOTksImV4cCI6MjA4ODE0MzI5OX0.dnVNSvcfLsfobzos6IxI2yxQXC6FU7jscrprf1evHKk",
        options: SupabaseClientOptions(
            auth: SupabaseClientOptions.AuthOptions(
                // We use DeviceID (UserDefaults) instead of Supabase Auth for now.
                // autoRefreshToken: false stops the SDK from attempting session
                // refreshes on launch. emitLocalSessionAsInitialSession: true
                // opts into the new SDK behavior that suppresses the startup warning.
                autoRefreshToken: false,
                emitLocalSessionAsInitialSession: true
            ),
            global: SupabaseClientOptions.GlobalOptions(
                session: session
            )
        )
    )

    // -------------------------------------------------------------------------
    // Retry helper — wraps any async throwing operation with exponential backoff.
    //
    // Only retries on transient network errors (NSURLErrorDomain codes below).
    // Non-network errors (Supabase 4xx, constraint violations, etc.) are
    // re-thrown immediately without retrying.
    // -------------------------------------------------------------------------

    @discardableResult
    static func withRetry<T>(
        maxAttempts: Int = 3,
        operation: () async throws -> T
    ) async throws -> T {
        var lastError: Error?

        for attempt in 1...maxAttempts {
            do {
                return try await operation()
            } catch let error as NSError where isTransientNetworkError(error) {
                lastError = error
                if attempt < maxAttempts {
                    // 1 s, 2 s, ... backoff
                    try? await Task.sleep(nanoseconds: UInt64(attempt) * 1_000_000_000)
                }
            }
            // Non-transient errors propagate immediately (no catch = rethrow)
        }

        throw lastError!
    }

    private static func isTransientNetworkError(_ error: NSError) -> Bool {
        guard error.domain == NSURLErrorDomain else { return false }
        // -1005 connection lost, -1001 timed out, -1004 cannot connect,
        // -1009 offline (brief drop), -1020 no cell data
        return [-1005, -1001, -1004, -1009, -1020].contains(error.code)
    }
}

// ---------------------------------------------------------------------------
// APNs token upload — called by AppDelegate after registration succeeds.
// ---------------------------------------------------------------------------

extension SupabaseManager {
    static func uploadAPNSToken(_ token: String) async {
        do {
            try await client
                .from("users")
                .update(["apn_token": token])
                .eq("id", value: DeviceID.current.uuidString)
                .execute()
            print("[BearBites] APNs token uploaded: \(token.prefix(8))...")
        } catch {
            print("[BearBites] Failed to upload APNs token: \(error.localizedDescription)")
        }
    }
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
