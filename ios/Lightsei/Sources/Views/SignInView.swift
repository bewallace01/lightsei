// Phase 29.2a + 30.2 + 31.5.x: sign-in surface.
//
// Two identities, one screen. A segmented control flips between:
//
//   Customer  → end-user paths (Sign in with Apple, magic-link via
//               email, paste-link fallback). Hits the existing
//               AuthStore.signIn(magicLinkToken:) / signInWithApple
//               flows.
//   Business  → operator magic-link via email + paste-link fallback,
//               hitting AuthStore.signInOperator(magicLinkToken:).
//               The legacy email + password path remains on
//               AuthStore.signInOperator(email:password:) for the
//               dashboard at app.lightsei.com but is not exposed in
//               the iOS UI as of 31.5.x.

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

    // Business state. Mirrors the customer-side magic-link flow:
    // request a link, then either tap the link in email (deep-link
    // path lives elsewhere) or paste the URL here as a fallback.
    @State private var bizEmail: String = ""
    @State private var bizSending: Bool = false
    @State private var bizSent: Bool = false
    @State private var bizSendError: String?
    @State private var bizPasted: String = ""
    @State private var bizConsuming: Bool = false
    @State private var bizPasteError: String?
    // Hidden password fallback. Disclosed behind a small "Use password
    // instead" link, kept so App Store reviewers + legacy operators
    // can sign in without round-tripping a magic link to email.
    @State private var bizShowPassword: Bool = false
    @State private var bizPassword: String = ""
    @State private var bizSigningInPassword: Bool = false
    @State private var bizPasswordError: String?

    var body: some View {
        ZStack {
            StarfieldBackground()
                .ignoresSafeArea()

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
                        bizRequestSection
                        divider
                        bizPasteSection
                        bizPasswordFallbackSection
                    }
                }
                .padding(24)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
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

    private var bizRequestSection: some View {
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
                .disabled(bizSending)

            if let bizSendError {
                Text(bizSendError).font(.caption).foregroundStyle(.red)
            }
            if bizSent {
                Text("Check your email for a sign-in link.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            Button {
                Task { await requestBizLink() }
            } label: {
                HStack(spacing: 8) {
                    if bizSending {
                        ProgressView()
                            .progressViewStyle(.circular)
                            .tint(.white)
                            .scaleEffect(0.8)
                    }
                    Text(bizSending ? "Sending…" : "Send magic link")
                        .fontWeight(.medium)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .foregroundStyle(.white)
                .background(Color.accentColor)
                .clipShape(RoundedRectangle(cornerRadius: 8))
                .opacity(canSendBizLink ? 1 : 0.6)
            }
            .disabled(!canSendBizLink)

            Text("Don't have a workspace yet? Sign up at app.lightsei.com, then come back here.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var canSendBizLink: Bool {
        !bizSending && !bizEmail.trimmingCharacters(
            in: .whitespacesAndNewlines).isEmpty
    }

    private var bizPasteSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Already have a sign-in link? Paste it here.")
                .font(.caption)
                .foregroundStyle(.secondary)

            TextField("https://app.lightsei.com/auth/magic-link?token=…",
                      text: $bizPasted, axis: .vertical)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .lineLimit(2...4)
                .padding(12)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(Color(.separator), lineWidth: 1),
                )
                .disabled(bizConsuming)

            if let bizPasteError {
                Text(bizPasteError).font(.caption).foregroundStyle(.red)
            }

            Button {
                Task { await consumeBizPasted() }
            } label: {
                HStack {
                    if bizConsuming { ProgressView() }
                    Text(bizConsuming ? "Signing in…" : "Sign in with pasted link")
                        .fontWeight(.medium)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .background(
                    RoundedRectangle(cornerRadius: 8)
                        .strokeBorder(Color(.separator), lineWidth: 1),
                )
            }
            .disabled(bizConsuming || bizPasted.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty)
        }
    }

    private func requestBizLink() async {
        let trimmed = bizEmail.trimmingCharacters(
            in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        bizSending = true
        bizSendError = nil
        defer { bizSending = false }
        do {
            try await auth.client.requestOperatorMagicLink(email: trimmed)
            bizSent = true
        } catch {
            bizSendError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }

    private func consumeBizPasted() async {
        guard let token = MagicLink.extractToken(from: bizPasted) else {
            bizPasteError = "Couldn't find a sign-in token in that. Paste the full link from your email."
            return
        }
        bizConsuming = true
        bizPasteError = nil
        defer { bizConsuming = false }
        do {
            try await auth.signInOperator(magicLinkToken: token)
        } catch {
            bizPasteError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }

    // MARK: legacy password fallback
    //
    // Hidden behind a small "Use password instead" disclosure so the
    // default UX stays magic-link-only. Kept for: (a) App Store
    // reviewers who can't easily process magic links, (b) legacy
    // operators who set up their workspace before magic-link was
    // exposed on iOS. Uses the bizEmail field shared with the magic-
    // link request section so the operator doesn't retype.
    private var bizPasswordFallbackSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            if !bizShowPassword {
                Button {
                    bizShowPassword = true
                } label: {
                    Text("Use password instead")
                        .font(.caption)
                        .foregroundStyle(Color.accentColor)
                }
            } else {
                Text("Sign in with your Lightsei password.")
                    .font(.caption)
                    .foregroundStyle(.secondary)

                SecureField("Password", text: $bizPassword)
                    .padding(12)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .strokeBorder(Color(.separator), lineWidth: 1),
                    )
                    .disabled(bizSigningInPassword)

                if let bizPasswordError {
                    Text(bizPasswordError).font(.caption).foregroundStyle(.red)
                }

                Button {
                    Task { await signInBusinessPassword() }
                } label: {
                    HStack(spacing: 8) {
                        if bizSigningInPassword {
                            ProgressView()
                                .progressViewStyle(.circular)
                                .tint(.white)
                                .scaleEffect(0.8)
                        }
                        Text(bizSigningInPassword ? "Signing in…" : "Sign in")
                            .fontWeight(.medium)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 12)
                    .foregroundStyle(.white)
                    .background(Color.accentColor)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .opacity(canSignInBizPassword ? 1 : 0.6)
                }
                .disabled(!canSignInBizPassword)

                Button {
                    bizShowPassword = false
                    bizPassword = ""
                    bizPasswordError = nil
                } label: {
                    Text("Use magic link instead")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.top, 8)
    }

    private var canSignInBizPassword: Bool {
        !bizSigningInPassword
            && !bizEmail.trimmingCharacters(
                in: .whitespacesAndNewlines).isEmpty
            && !bizPassword.isEmpty
    }

    private func signInBusinessPassword() async {
        let trimmed = bizEmail.trimmingCharacters(
            in: .whitespacesAndNewlines)
        bizSigningInPassword = true
        bizPasswordError = nil
        defer { bizSigningInPassword = false }
        do {
            try await auth.signInOperator(
                email: trimmed, password: bizPassword,
            )
        } catch {
            bizPasswordError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }
}

#Preview {
    SignInView().environmentObject(AuthStore())
}
