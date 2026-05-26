// Phase 29.2c stub: typed wrapper for POST /auth/end-user/sign-in-with-apple.
//
// The iOS-side SignInWithApple wrapper calls this with the
// identityToken from ASAuthorizationAppleIDCredential plus the
// optional email + display_name (Apple sends those only on first
// sign-in). Backend returns 501 today; once Bailey's Developer
// account is set up + apple_signin.verify_identity_token is
// implemented, the same wrapper returns a real session_token.

import Foundation

struct SignInWithAppleRequest: Encodable {
    let identity_token: String
    let email: String?
    let display_name: String?
}

extension APIClient {
    func signInWithApple(
        identityToken: String,
        email: String?,
        displayName: String?,
    ) async throws -> MagicLinkConsumeResponse {
        try await request(
            "auth/end-user/sign-in-with-apple",
            method: "POST",
            body: SignInWithAppleRequest(
                identity_token: identityToken,
                email: email,
                display_name: displayName,
            ),
        )
    }
}
