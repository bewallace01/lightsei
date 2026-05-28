// Phase 29.4 stub: ask iOS for permission + register for APNS.
//
// SwiftUI doesn't expose the APNS device-token callback so we
// route it through a UIKit AppDelegate adapter (see
// LightseiAppDelegate). The adapter calls
// PushRegistration.shared.handleDeviceToken(...) which forwards
// to the AuthStore for POST /me/end-user/apns-tokens.
//
// Today this code is INERT in the simulator + on a real device
// because:
//   1. The Push Notifications capability is not in the entitlement
//      (can't be without Apple Developer Team ID).
//   2. The backend's register endpoint succeeds but no APNS
//      gateway picks up the token until LIGHTSEI_APNS_* env vars
//      are configured (apns.py is a capture-mode stub).
//
// Calling request() in code is safe: iOS surfaces a permission
// prompt + registerForRemoteNotifications returns the error path
// without crashing.

import Security
import UIKit

@MainActor
final class PushRegistration: ObservableObject {
    static let shared = PushRegistration()

    private var auth: AuthStore?

    func attach(authStore: AuthStore) {
        self.auth = authStore
    }

    /// Ask iOS for notification permission + register for APNS.
    /// Called once on signed-in launch. No-op if the user denied
    /// (permission only re-asked from Settings).
    func request() {
        Task {
            let center = UNUserNotificationCenter.current()
            do {
                let granted = try await center.requestAuthorization(
                    options: [.alert, .badge, .sound],
                )
                guard granted else { return }
                await MainActor.run {
                    UIApplication.shared.registerForRemoteNotifications()
                }
            } catch {
                // Surface to logs only; the user can re-trigger via
                // a Settings deep-link later (Phase 29.4b polish).
                print("[Lightsei] notif auth failed: \(error)")
            }
        }
    }

    /// Called from LightseiAppDelegate's
    /// didRegisterForRemoteNotificationsWithDeviceToken. Forwards
    /// the hex-encoded token to the backend.
    func handleDeviceToken(_ data: Data) {
        guard let auth else { return }
        let hex = data.map { String(format: "%02x", $0) }.joined()
        let bundleID = Bundle.main.bundleIdentifier ?? "com.lightsei.app"
        let environment = apnsEnvironment()
        Task {
            do {
                _ = try await auth.client.registerAPNSToken(
                    deviceToken: hex,
                    bundleID: bundleID,
                    environment: environment,
                )
            } catch {
                print("[Lightsei] APNS register failed: \(error)")
            }
        }
    }

    private func apnsEnvironment() -> String {
        guard let task = SecTaskCreateFromSelf(nil),
              let value = SecTaskCopyValueForEntitlement(
                task,
                "aps-environment" as CFString,
                nil,
              ) as? String,
              value == "production"
        else {
            return "sandbox"
        }
        return "production"
    }
}
