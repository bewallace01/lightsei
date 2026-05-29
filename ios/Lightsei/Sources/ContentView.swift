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
            SlackShellView(endUser: user)
        }
    }
}

private struct SplashView: View {
    var body: some View {
        ZStack {
            Color(.systemBackground).ignoresSafeArea()
            VStack(spacing: 14) {
                // Indigo rounded square mirroring the home-screen
                // icon so the first frame in-app reads as
                // "the app I just tapped, loading."
                ZStack {
                    RoundedRectangle(cornerRadius: 16)
                        .fill(Color.accentColor)
                    Text("L")
                        .font(.system(
                            size: 36, weight: .semibold,
                        ))
                        .foregroundStyle(.white)
                }
                .frame(width: 72, height: 72)

                Text("Lightsei")
                    .font(.system(
                        size: 22, weight: .semibold, design: .serif,
                    ))
                ProgressView()
                    .controlSize(.small)
                    .padding(.top, 4)
            }
        }
    }
}

#Preview {
    ContentView().environmentObject(AuthStore())
}
