// Phase 29.2a: observable auth state for the app.
//
// One source of truth for "am I signed in?" + the EndUser profile.
// LightseiApp creates one AuthStore at launch, ContentView switches
// on `state`, the sign-in flows call signIn(token:) to flip to .ok.
//
// Persists the session token to the Keychain via Keychain.read/
// write/clear. Restores on launch via `restore()`.

import Foundation

@MainActor
final class AuthStore: ObservableObject {
    enum State {
        case unknown          // before restore() runs
        case signedOut
        case ok(EndUser)
    }

    @Published private(set) var state: State = .unknown

    private var api: APIClient = .production

    /// Re-hydrate from Keychain + verify the token against the
    /// backend via GET /me/end-user. Called once at app launch.
    func restore() async {
        guard let token = Keychain.read() else {
            state = .signedOut
            return
        }
        var client = api
        client.bearer = token
        do {
            let me = try await client.fetchEndUserMe()
            guard Keychain.read() == token else { return }
            api.bearer = token
            state = .ok(me.end_user)
        } catch APIError.unauthorized {
            if Keychain.read() == token {
                Keychain.clear()
                state = .signedOut
            }
        } catch {
            if Keychain.read() == token {
                // Network blip on launch: keep the token for the
                // next restore attempt.
                state = .signedOut
            }
        }
    }

    /// Sign in by consuming a magic-link token. Persists the
    /// resulting session token + flips state.
    func signIn(
        magicLinkToken: String,
        vendorInviteCode: String? = nil,
    ) async throws {
        let resp = try await api.consumeMagicLink(
            token: magicLinkToken,
            vendorInviteCode: vendorInviteCode,
        )
        try acceptSession(token: resp.session_token, endUser: resp.end_user)
    }

    func signOut() {
        Keychain.clear()
        api.bearer = nil
        state = .signedOut
    }

    /// Bearer-attached client for callers (e.g. SignInView for the
    /// magic-link request, future vendor list fetches).
    var client: APIClient { api }

    /// Set the bearer + flip to .ok. Used by auxiliary sign-in
    /// flows (Sign in with Apple) that live in extension files
    /// and so can't poke the private `api` field directly.
    func acceptSession(token: String, endUser: EndUser) throws {
        try Keychain.write(token)
        api.bearer = token
        state = .ok(endUser)
        PushRegistration.shared.request()
    }
}
