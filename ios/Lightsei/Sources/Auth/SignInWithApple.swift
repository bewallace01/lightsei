// Phase 29.2c stub: Sign in with Apple coordinator.
//
// Wraps ASAuthorizationController so the SwiftUI sign-in view can
// kick off the OS auth sheet with one async call. On success, the
// coordinator extracts the identity token + optional email +
// fullName from the credential, then calls signInWithApple on the
// AuthStore's APIClient.
//
// Today the backend returns 501 (the verify path is stubbed
// pending Apple Developer account setup), so a successful Apple
// auth still surfaces an "siwa_not_configured" error in the UI.
// The button + coordinator stay live so the user-flow shape is
// frozen now; flipping live is just a backend implementation
// change.

import AuthenticationServices
import SwiftUI
import UIKit

@MainActor
final class SignInWithAppleCoordinator: NSObject,
    ASAuthorizationControllerDelegate,
    ASAuthorizationControllerPresentationContextProviding {

    private var continuation:
        CheckedContinuation<ASAuthorizationAppleIDCredential, Error>?

    /// Present the system Sign in with Apple sheet + await the
    /// credential. Throws if the user cancels or the OS reports
    /// an error.
    func requestCredential() async throws
        -> ASAuthorizationAppleIDCredential {
        try await withCheckedThrowingContinuation { cont in
            self.continuation = cont
            let provider = ASAuthorizationAppleIDProvider()
            let request = provider.createRequest()
            request.requestedScopes = [.email, .fullName]
            let controller = ASAuthorizationController(
                authorizationRequests: [request],
            )
            controller.delegate = self
            controller.presentationContextProvider = self
            controller.performRequests()
        }
    }

    // MARK: - ASAuthorizationControllerDelegate

    func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithAuthorization authorization: ASAuthorization,
    ) {
        defer { continuation = nil }
        guard let cred = authorization.credential as?
            ASAuthorizationAppleIDCredential
        else {
            continuation?.resume(throwing: SignInWithAppleError.unexpectedCredential)
            return
        }
        continuation?.resume(returning: cred)
    }

    func authorizationController(
        controller: ASAuthorizationController,
        didCompleteWithError error: Error,
    ) {
        defer { continuation = nil }
        continuation?.resume(throwing: error)
    }

    // MARK: - PresentationContextProviding

    func presentationAnchor(
        for controller: ASAuthorizationController,
    ) -> ASPresentationAnchor {
        // Surface the active key window for the OS sheet. The
        // first window in the foreground scene is the right one
        // 99% of the time on iPhone; iPad multi-window edge case
        // is unlikely to hit the consumer surface.
        guard let scene = UIApplication.shared.connectedScenes
            .first(where: { $0.activationState == .foregroundActive })
                as? UIWindowScene,
            let window = scene.windows.first
        else {
            return ASPresentationAnchor()
        }
        return window
    }
}

enum SignInWithAppleError: Error, LocalizedError {
    case unexpectedCredential
    case missingIdentityToken

    var errorDescription: String? {
        switch self {
        case .unexpectedCredential:
            return "Sign in with Apple returned an unexpected credential type."
        case .missingIdentityToken:
            return "Sign in with Apple didn't return an identity token."
        }
    }
}

extension AuthStore {
    /// Phase 29.2c stub: run the full Sign in with Apple flow
    /// (present sheet → POST to backend → persist session). The
    /// backend currently returns 501; the coordinator is wired so
    /// the user-flow shape is frozen.
    func signInWithApple() async throws {
        let coordinator = SignInWithAppleCoordinator()
        let cred = try await coordinator.requestCredential()
        guard let tokenData = cred.identityToken,
              let token = String(data: tokenData, encoding: .utf8)
        else {
            throw SignInWithAppleError.missingIdentityToken
        }
        let fullName = cred.fullName.flatMap { name -> String? in
            let parts = [name.givenName, name.familyName]
                .compactMap { $0 }
                .filter { !$0.isEmpty }
            return parts.isEmpty ? nil : parts.joined(separator: " ")
        }
        let resp = try await client.signInWithApple(
            identityToken: token,
            email: cred.email,
            displayName: fullName,
        )
        try acceptSession(token: resp.session_token, endUser: resp.end_user)
    }
}
