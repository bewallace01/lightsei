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
                // Phase 31.5.x: Lightsei brand identity is a deep
                // cosmos. Lock the iOS app to dark mode so every
                // surface inherits the indigo/black palette + the
                // accent pops as a bright star against a dark sky.
                // (Light mode is parked as a future-toggle if any
                // operator screams; the brand is dark-first.)
                .preferredColorScheme(.dark)
                .environmentObject(auth)
                .task {
                    await auth.restore()
                    PushRegistration.shared.attach(authStore: auth)
                    // Only ask once we know who the user is —
                    // pre-signin the request would block the
                    // sign-in surface for no benefit.
                    switch auth.state {
                    case .endUser, .operatorUser:
                        PushRegistration.shared.request()
                    default:
                        break
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
            NSLog("[Lightsei] unhandled URL: %@", url.absoluteString)
            return
        }
        Task {
            do {
                try await auth.signIn(magicLinkToken: token)
            } catch {
                // NSLog over print so it surfaces in `xcrun simctl
                // log show` + Console.app without attaching Xcode.
                NSLog("[Lightsei] magic-link consume failed: %@",
                      String(describing: error))
            }
        }
    }
}
