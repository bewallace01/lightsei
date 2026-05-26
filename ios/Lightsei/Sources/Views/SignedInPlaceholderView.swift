// Phase 29.2a: tiny placeholder shown after sign-in.
//
// Just confirms the auth flow worked and exposes a sign-out button
// so we can verify state transitions both ways. Phase 29.3 replaces
// this with the real vendor list + chat surface.

import SwiftUI

struct SignedInPlaceholderView: View {
    @EnvironmentObject var auth: AuthStore
    let endUser: EndUser

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "checkmark.circle.fill")
                .font(.system(size: 48))
                .foregroundStyle(.tint)
            Text("Signed in")
                .font(.system(size: 24, weight: .semibold))
            Text(endUser.email)
                .font(.system(size: 14))
                .foregroundStyle(.secondary)

            Button("Sign out", role: .destructive) {
                auth.signOut()
            }
            .padding(.top, 24)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(.systemBackground).ignoresSafeArea())
    }
}
