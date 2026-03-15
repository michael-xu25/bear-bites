import SwiftUI
import UserNotifications

@main
struct Bear_BitesApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
        }
    }
}

class AppDelegate: NSObject, UIApplicationDelegate {

    func application(
        _ app: UIApplication,
        didFinishLaunchingWithOptions _: [UIApplication.LaunchOptionsKey: Any]? = nil
    ) -> Bool {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
            if let error {
                print("[BearBites] Notification permission error: \(error.localizedDescription)")
                return
            }
            if granted {
                DispatchQueue.main.async { app.registerForRemoteNotifications() }
            } else {
                print("[BearBites] Notification permission denied by user.")
            }
        }
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data
    ) {
        let token = deviceToken.map { String(format: "%02x", $0) }.joined()
        print("[BearBites] APNs token received: \(token.prefix(8))...")
        Task { await SupabaseManager.uploadAPNSToken(token) }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error
    ) {
        print("[BearBites] APNs registration failed: \(error.localizedDescription)")
    }
}
