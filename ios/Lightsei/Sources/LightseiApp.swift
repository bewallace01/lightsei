// Phase 29.1: app entry point.
//
// SwiftUI lifecycle (no UIApplicationDelegate yet). Phase 29.2 adds a
// scene-phase observer + URL handler for the magic-link sign-in flow.
// Phase 29.4 adds an AppDelegate for APNS device-token registration
// (UNUserNotificationCenter doesn't cover the silent push case).

import SwiftUI

@main
struct LightseiApp: App {
    var body: some Scene {
        WindowGroup {
            ContentView()
                // Custom URL scheme handler. Phase 29.2 swaps the body
                // for the real magic-link consume flow; today this is
                // just a breadcrumb so we can verify the deep-link
                // pipe works end-to-end in the simulator.
                .onOpenURL { url in
                    print("[Lightsei] opened via URL: \(url.absoluteString)")
                }
        }
    }
}
