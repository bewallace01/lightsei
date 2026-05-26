// Phase 29.2a: sign-in surface.
//
// Two paths to a signed-in state:
//
//   1. Send a magic link to my email — the standard flow. User
//      taps the link in Mail; on the simulator we don't yet route
//      that into the app (29.2b adds universal links), so today
//      they fall back to:
//   2. Paste my sign-in link — same fallback as the /c web page.
//      Lets a user signed-out in the PWA / iOS app finish auth
//      without leaving the app.
//
// 29.2c will add a "Sign in with Apple" button above the form.

import SwiftUI
import AuthenticationServices

struct SignInView: View {
    @EnvironmentObject var auth: AuthStore

    @State private var email: String = ""
    @State private var sending: Bool = false
    @State private var sent: Bool = false
    @State private var sendError: String?

    @State private var pasted: String = ""
    @State private var consuming: Bool = false
    @State private var pasteError: String?

    // Phase 29.2c stub: Sign in with Apple.
    @State private var siwaError: String?
    @State private var siwaBusy: Bool = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header

                siwaSection

                divider

                requestSection

                divider

                pasteSection
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(.systemBackground).ignoresSafeArea())
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Sign in").font(.system(size: 28, weight: .semibold))
            Text("Continue with Apple, or get a magic-link by email.")
                .font(.system(size: 14))
                .foregroundStyle(.secondary)
        }
    }

    // Phase 29.2c stub: Sign in with Apple button + coordinator.
    // The button is the canonical SignInWithAppleButton; tapping
    // it presents the system sheet via SignInWithAppleCoordinator.
    // Backend currently returns 501 ("siwa_not_configured") so a
    // successful auth still surfaces an inline error — the surface
    // is here so flipping live is just a backend change.
    private var siwaSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SignInWithAppleButton(.signIn) { request in
                request.requestedScopes = [.email, .fullName]
            } onCompletion: { _ in
                // The system gives us a result here, but routing
                // through AuthStore.signInWithApple keeps the
                // identity-token plumbing in one place + lets the
                // coordinator handle presentation. We ignore this
                // callback's result and trigger our own flow on
                // tap below via a simultaneous gesture, OR (simpler
                // here) trigger via the wrapper directly. Apple
                // requires the official button for App Store
                // approval, so we keep it as the visible control.
            }
            .signInWithAppleButtonStyle(.black)
            .frame(height: 48)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            // Overlay an invisible button so we can hand the flow
            // off to our coordinator (which surfaces backend
            // errors). The native SignInWithAppleButton's
            // onCompletion doesn't give us a clean async hook
            // for the network round-trip.
            .overlay(
                Button(action: { Task { await runSiwa() } }) {
                    Color.clear
                }
                .disabled(siwaBusy),
            )

            if siwaBusy {
                ProgressView()
                    .controlSize(.small)
                    .frame(maxWidth: .infinity, alignment: .center)
            }
            if let siwaError {
                Text(siwaError).font(.caption).foregroundStyle(.red)
            }
        }
    }

    private func runSiwa() async {
        siwaBusy = true
        siwaError = nil
        defer { siwaBusy = false }
        do {
            try await auth.signInWithApple()
        } catch {
            siwaError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }

    private var requestSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            TextField("you@example.com", text: $email)
                .textInputAutocapitalization(.never)
                .keyboardType(.emailAddress)
                .autocorrectionDisabled()
                .padding(12)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(Color(.separator), lineWidth: 1),
                )
                .disabled(sending)

            if let sendError {
                Text(sendError).font(.caption).foregroundStyle(.red)
            }

            if sent {
                Text("Sent. Tap the link in your email or paste it below.")
                    .font(.caption)
                    .foregroundStyle(.green)
            }

            Button {
                Task { await requestLink() }
            } label: {
                HStack {
                    if sending { ProgressView().tint(.white) }
                    Text(sending ? "Sending…" : "Send magic link")
                        .fontWeight(.medium)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .foregroundStyle(.white)
                .background(Color.accentColor)
                .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            .disabled(sending || email.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty)
        }
    }

    private var divider: some View {
        HStack(spacing: 12) {
            Rectangle().fill(Color(.separator)).frame(height: 1)
            Text("OR")
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
            Rectangle().fill(Color(.separator)).frame(height: 1)
        }
    }

    private var pasteSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Already have a sign-in link? Paste it here.")
                .font(.caption)
                .foregroundStyle(.secondary)

            TextField("https://app.lightsei.com/c/auth/magic-link?token=…",
                      text: $pasted, axis: .vertical)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .lineLimit(2...4)
                .padding(12)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(Color(.separator), lineWidth: 1),
                )
                .disabled(consuming)

            if let pasteError {
                Text(pasteError).font(.caption).foregroundStyle(.red)
            }

            Button {
                Task { await consumePasted() }
            } label: {
                HStack {
                    if consuming { ProgressView() }
                    Text(consuming ? "Signing in…" : "Sign in with pasted link")
                        .fontWeight(.medium)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(Color(.separator), lineWidth: 1),
                )
            }
            .disabled(consuming || pasted.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty)
        }
    }

    private func requestLink() async {
        let trimmed = email.trimmingCharacters(
            in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        sending = true
        sendError = nil
        defer { sending = false }
        do {
            try await auth.client.requestMagicLink(email: trimmed)
            sent = true
        } catch {
            sendError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }

    private func consumePasted() async {
        guard let token = MagicLink.extractToken(from: pasted) else {
            pasteError = "Couldn't find a sign-in token in that. Paste the full link from your email."
            return
        }
        consuming = true
        pasteError = nil
        defer { consuming = false }
        do {
            try await auth.signIn(magicLinkToken: token)
        } catch {
            pasteError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }
}

#Preview {
    SignInView().environmentObject(AuthStore())
}
