// Phase 29.2a/b: app entry point with auth store + deep-link handler.
//
// Owns the single AuthStore instance for the app and runs `restore()`
// once at launch so a returning user lands on the signed-in surface
// without seeing the sign-in form flash. Two deep-link paths consume
// magic-link tokens:
//
//   1. .onOpenURL          custom scheme (`lightsei://auth/...`) +
//                          legacy fallback when AASA hasn't
//                          propagated yet.
//   2. .onContinueUserActivity   universal links (Phase 29.2b).
//                          iOS routes taps on
//                          https://app.lightsei.com/c/auth/magic-link?token=…
//                          here when the AASA file at
//                          /.well-known/apple-app-site-association
//                          validates the appID.

import SwiftUI

@main
struct LightseiApp: App {
    @StateObject private var auth = AuthStore()
    // Phase 29.4 stub: UIApplicationDelegate adapter for the APNS
    // device-token callback. SwiftUI's lifecycle doesn't fire
    // didRegisterForRemoteNotificationsWithDeviceToken directly so
    // we bridge through LightseiAppDelegate.
    @UIApplicationDelegateAdaptor(LightseiAppDelegate.self)
        var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView()
                // Force the asset-catalog accent at the root. The
                // ASSETCATALOG_COMPILER_GLOBAL_ACCENT_COLOR_NAME
                // build setting wires the catalog but SwiftUI's
                // Color.accentColor still falls back to systemBlue
                // in some places (notably buttons + .tint shorthand)
                // unless we explicitly inject our color.
                .tint(Color("AccentColor"))
                .environmentObject(auth)
                .task {
                    PushRegistration.shared.attach(authStore: auth)
                    await auth.restore()
                    // Only ask once we know who the user is,
                    // pre-signin the request would block the
                    // sign-in surface for no benefit.
                    if case .ok = auth.state {
                        PushRegistration.shared.request()
                    }
                }
                .onOpenURL { url in
                    handleIncomingURL(url)
                }
                .onContinueUserActivity(
                    NSUserActivityTypeBrowsingWeb,
                ) { activity in
                    if let url = activity.webpageURL {
                        handleIncomingURL(url)
                    }
                }
        }
    }

    private func handleIncomingURL(_ url: URL) {
        guard let token = MagicLink.extractToken(from: url.absoluteString) else {
            // Unknown URL shape: log + ignore. Better than crashing
            // or silently signing the user out.
            print("[Lightsei] unhandled URL: \(url.absoluteString)")
            return
        }
        Task {
            do {
                try await auth.signIn(magicLinkToken: token)
            } catch {
                print("[Lightsei] consume failed: \(error)")
            }
        }
    }
}
