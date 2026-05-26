// Phase 29.4 stub: UIKit AppDelegate bridge.
//
// SwiftUI's App protocol can't receive the APNS device-token
// callback (didRegisterForRemoteNotificationsWithDeviceToken)
// directly — UIApplicationDelegate is the only surface that
// fires. LightseiApp registers this adapter via
// @UIApplicationDelegateAdaptor so the SwiftUI lifecycle stays
// canonical for everything else but APNS still works.

import UIKit
import UserNotifications

final class LightseiAppDelegate: NSObject, UIApplicationDelegate,
    UNUserNotificationCenterDelegate {

    func application(
        _ application: UIApplication,
        didFinishLaunchingWithOptions launchOptions: [
            UIApplication.LaunchOptionsKey: Any
        ]? = nil,
    ) -> Bool {
        // Wire ourselves as the notification center delegate so
        // foreground + tap callbacks land here. PushRegistration
        // handles the token; this delegate handles presentation +
        // tap routing (Phase 29.4b).
        UNUserNotificationCenter.current().delegate = self
        return true
    }

    func application(
        _ application: UIApplication,
        didRegisterForRemoteNotificationsWithDeviceToken
            deviceToken: Data,
    ) {
        Task { @MainActor in
            PushRegistration.shared.handleDeviceToken(deviceToken)
        }
    }

    func application(
        _ application: UIApplication,
        didFailToRegisterForRemoteNotificationsWithError error: Error,
    ) {
        // Common in simulator (no APNS) and on devices without the
        // Push Notifications entitlement. Logged only.
        print("[Lightsei] APNS registration failed: \(error)")
    }

    // Foreground presentation: show banner + sound. iOS suppresses
    // notifications for the foreground app by default; this lets
    // ours show. Phase 29.4b can swap to in-app toasts.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler:
            @escaping (UNNotificationPresentationOptions) -> Void,
    ) {
        completionHandler([.banner, .sound, .badge])
    }

    // Tap routing: Phase 29.4b reads `deep_link_url` from the
    // notification's userInfo + routes via the same MagicLink-style
    // bridge LightseiApp uses for universal links. Today: logged
    // only.
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void,
    ) {
        let url =
            response.notification.request.content.userInfo["deep_link_url"]
        print("[Lightsei] notification tap, deep_link_url=\(url ?? "nil")")
        completionHandler()
    }
}
