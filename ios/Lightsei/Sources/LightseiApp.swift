// Phase 29.2a: app entry point with auth store + deep-link handler.
//
// Owns the single AuthStore instance for the app and runs `restore()`
// once at launch so a returning user lands on the signed-in surface
// without seeing the sign-in form flash. Custom URL scheme deep links
// (`lightsei://auth/magic-link?token=…`) consume into the auth store.
// 29.2b adds universal-link handling via `.onContinueUserActivity`.

import SwiftUI

@main
struct LightseiApp: App {
    @StateObject private var auth = AuthStore()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environmentObject(auth)
                .task { await auth.restore() }
                .onOpenURL { url in
                    handleMagicLinkURL(url)
                }
        }
    }

    private func handleMagicLinkURL(_ url: URL) {
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
