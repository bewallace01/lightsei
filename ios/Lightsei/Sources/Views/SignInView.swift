// Phase 29.2a + 30.2: sign-in surface.
//
// Two identities, one screen. A segmented control flips between:
//
//   Customer  → end-user paths (Sign in with Apple, magic-link via
//               email, paste-link fallback). Hits the existing
//               AuthStore.signIn(magicLinkToken:) / signInWithApple
//               flows.
//   Business  → operator email + password against POST /auth/login,
//               flips AuthStore into .operatorUser on success.

import SwiftUI
import AuthenticationServices

struct SignInView: View {
    @EnvironmentObject var auth: AuthStore

    enum Mode: String, CaseIterable, Identifiable {
        case customer = "Customer"
        case business = "Business"
        var id: Self { self }
    }

    @State private var mode: Mode = .customer

    // Customer state.
    @State private var email: String = ""
    @State private var sending: Bool = false
    @State private var sent: Bool = false
    @State private var sendError: String?
    @State private var pasted: String = ""
    @State private var consuming: Bool = false
    @State private var pasteError: String?
    @State private var siwaError: String?
    @State private var siwaBusy: Bool = false

    // Business state.
    @State private var bizEmail: String = ""
    @State private var bizPassword: String = ""
    @State private var bizSigningIn: Bool = false
    @State private var bizError: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                header
                modePicker

                if mode == .customer {
                    siwaSection
                    divider
                    requestSection
                    divider
                    pasteSection
                } else {
                    businessSection
                }
            }
            .padding(24)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(.systemBackground).ignoresSafeArea())
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Sign in").font(.system(size: 28, weight: .semibold))
            Text(mode == .customer
                 ? "Continue with Apple, or get a magic-link by email."
                 : "Sign in to your Lightsei workspace.")
                .font(.system(size: 14))
                .foregroundStyle(.secondary)
        }
    }

    private var modePicker: some View {
        Picker("Mode", selection: $mode) {
            ForEach(Mode.allCases) { m in
                Text(m.rawValue).tag(m)
            }
        }
        .pickerStyle(.segmented)
    }

    // MARK: customer lane

    private var siwaSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            SignInWithAppleButton(.signIn) { request in
                request.requestedScopes = [.email, .fullName]
            } onCompletion: { _ in
                // Trigger flow via the wrapper below, not the
                // system callback, so we can surface backend errors.
            }
            .signInWithAppleButtonStyle(.black)
            .frame(height: 48)
            .clipShape(RoundedRectangle(cornerRadius: 8))
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
                HStack(spacing: 8) {
                    if sending {
                        ProgressView()
                            .progressViewStyle(.circular)
                            .tint(.white)
                            .scaleEffect(0.8)
                    }
                    Text(sending ? "Sending…" : "Send magic link")
                        .fontWeight(.medium)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .foregroundStyle(.white)
                .background(Color.accentColor)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .opacity(canSendLink ? 1 : 0.6)
            }
            .disabled(!canSendLink)
        }
    }

    private var canSendLink: Bool {
        !sending && !email.trimmingCharacters(
            in: .whitespacesAndNewlines).isEmpty
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

    // MARK: business lane

    private var businessSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            TextField("Work email", text: $bizEmail)
                .textInputAutocapitalization(.never)
                .keyboardType(.emailAddress)
                .autocorrectionDisabled()
                .padding(12)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(Color(.separator), lineWidth: 1),
                )
                .disabled(bizSigningIn)

            SecureField("Password", text: $bizPassword)
                .padding(12)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(Color(.separator), lineWidth: 1),
                )
                .disabled(bizSigningIn)

            if let bizError {
                Text(bizError).font(.caption).foregroundStyle(.red)
            }

            Button {
                Task { await signInBusiness() }
            } label: {
                HStack(spacing: 8) {
                    if bizSigningIn {
                        ProgressView()
                            .progressViewStyle(.circular)
                            .tint(.white)
                            .scaleEffect(0.8)
                    }
                    Text(bizSigningIn ? "Signing in…" : "Sign in")
                        .fontWeight(.medium)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .foregroundStyle(.white)
                .background(Color.accentColor)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .opacity(canSignInBiz ? 1 : 0.6)
            }
            .disabled(!canSignInBiz)

            Text("Don't have a workspace yet? Sign up at app.lightsei.com, then come back here.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var canSignInBiz: Bool {
        !bizSigningIn
            && !bizEmail.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty
            && !bizPassword.isEmpty
    }

    private func signInBusiness() async {
        let trimmed = bizEmail.trimmingCharacters(
            in: .whitespacesAndNewlines)
        bizSigningIn = true
        bizError = nil
        defer { bizSigningIn = false }
        do {
            try await auth.signInOperator(
                email: trimmed, password: bizPassword,
            )
        } catch {
            bizError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }
}

#Preview {
    SignInView().environmentObject(AuthStore())
}
