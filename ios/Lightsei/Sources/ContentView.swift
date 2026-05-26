// Phase 29.2a: root view, switches on the AuthStore state.
//
// Three cases:
//
//   .unknown    → splash while restore() runs (avoids a flash of
//                 SignInView for returning users).
//   .signedOut  → SignInView.
//   .ok(user)   → SignedInPlaceholderView (29.3 replaces this with
//                 the real vendor-list + chat surface).

import SwiftUI

struct ContentView: View {
    @EnvironmentObject var auth: AuthStore

    var body: some View {
        switch auth.state {
        case .unknown:
            SplashView()
        case .signedOut:
            SignInView()
        case .ok(let user):
            VendorListView(endUser: user)
        }
    }
}

private struct SplashView: View {
    var body: some View {
        ZStack {
            Color(.systemBackground).ignoresSafeArea()
            VStack(spacing: 16) {
                Image(systemName: "sparkle")
                    .font(.system(size: 64, weight: .light))
                    .foregroundStyle(.tint)
                Text("Lightsei")
                    .font(.system(size: 28, weight: .semibold, design: .serif))
            }
        }
    }
}

#Preview {
    ContentView().environmentObject(AuthStore())
}
