// Phase 30.2: dual-identity auth state.
//
// One AuthStore serves both signed-in identities the Lightsei iOS app
// supports:
//
//   - End user   — the customer of someone else's Constellation. Auth
//                  via magic-link or Sign in with Apple; session
//                  stored in the end-user Keychain slot.
//   - Operator   — the business owner of a workspace. Auth via email
//                  + password (POST /auth/login); session stored in
//                  the operator Keychain slot.
//
// Only one identity is active at a time. The dormant slot is kept so
// a future account-switcher (also Phase 30) can flip identities
// without re-authenticating.
//
// `restore()` rehydrates from whichever slot the user last used (a
// UserDefaults pointer), falling back to the end-user slot then the
// operator slot if the pointer is missing.

import Foundation

@MainActor
final class AuthStore: ObservableObject {
    enum State {
        case unknown                                  // before restore()
        case signedOut
        case endUser(EndUser)
        case operatorUser(OperatorIdentity)
    }

    /// Wraps the operator user + their active workspace. Persisted
    /// in-memory only; restored each launch via /auth/me.
    struct OperatorIdentity: Equatable {
        let user: OperatorUser
        let workspace: OperatorWorkspace?
    }

    @Published private(set) var state: State = .unknown

    private var api: APIClient = .production

    private static let lastIdentityKey = "com.lightsei.app.lastIdentity"
    private enum LastIdentity: String { case endUser, operatorUser }

    func restore() async {
        let last = LastIdentity(
            rawValue: UserDefaults.standard.string(
                forKey: Self.lastIdentityKey,
            ) ?? "",
        )

        // Try the preferred slot first; fall through to the other.
        let order: [LastIdentity] = last == .operatorUser
            ? [.operatorUser, .endUser]
            : [.endUser, .operatorUser]

        for identity in order {
            switch identity {
            case .endUser:
                if await tryRestoreEndUser() { return }
            case .operatorUser:
                if await tryRestoreOperator() { return }
            }
        }
        state = .signedOut
    }

    private func tryRestoreEndUser() async -> Bool {
        guard let token = Keychain.read(account: Keychain.endUserAccount) else {
            return false
        }
        var client = api
        client.bearer = token
        do {
            let me = try await client.fetchEndUserMe()
            api.bearer = token
            state = .endUser(me.end_user)
            UserDefaults.standard.set(
                LastIdentity.endUser.rawValue, forKey: Self.lastIdentityKey,
            )
            return true
        } catch APIError.unauthorized {
            Keychain.clear(account: Keychain.endUserAccount)
            return false
        } catch {
            // Network blip: don't clear the token, just treat as not
            // restorable this launch.
            return false
        }
    }

    private func tryRestoreOperator() async -> Bool {
        guard let token = Keychain.read(account: Keychain.operatorAccount) else {
            return false
        }
        var client = api
        client.bearer = token
        do {
            let me = try await client.fetchOperatorAuthMe()
            guard let user = me.user else {
                // /auth/me succeeded but with no user: the token is
                // an API key or something else. Wipe + give up.
                Keychain.clear(account: Keychain.operatorAccount)
                return false
            }
            api.bearer = token
            state = .operatorUser(
                OperatorIdentity(user: user, workspace: me.workspace),
            )
            UserDefaults.standard.set(
                LastIdentity.operatorUser.rawValue,
                forKey: Self.lastIdentityKey,
            )
            return true
        } catch APIError.unauthorized {
            Keychain.clear(account: Keychain.operatorAccount)
            return false
        } catch {
            return false
        }
    }

    // MARK: end-user sign-in

    func signIn(magicLinkToken: String) async throws {
        let resp = try await api.consumeMagicLink(token: magicLinkToken)
        try Keychain.write(
            resp.session_token, account: Keychain.endUserAccount,
        )
        api.bearer = resp.session_token
        state = .endUser(resp.end_user)
        UserDefaults.standard.set(
            LastIdentity.endUser.rawValue, forKey: Self.lastIdentityKey,
        )
    }

    func acceptSession(token: String, endUser: EndUser) throws {
        try Keychain.write(token, account: Keychain.endUserAccount)
        api.bearer = token
        state = .endUser(endUser)
        UserDefaults.standard.set(
            LastIdentity.endUser.rawValue, forKey: Self.lastIdentityKey,
        )
    }

    // MARK: operator sign-in

    func signInOperator(email: String, password: String) async throws {
        let resp = try await api.operatorLogin(
            email: email, password: password,
        )
        try Keychain.write(
            resp.session_token, account: Keychain.operatorAccount,
        )
        api.bearer = resp.session_token
        state = .operatorUser(
            OperatorIdentity(user: resp.user, workspace: resp.workspace),
        )
        UserDefaults.standard.set(
            LastIdentity.operatorUser.rawValue,
            forKey: Self.lastIdentityKey,
        )
    }

    // Phase 31.5.x: parity with `signIn(magicLinkToken:)` for end
    // users. Consumes an operator magic-link token + stores the
    // returned session in the operator keychain slot.
    func signInOperator(magicLinkToken token: String) async throws {
        let resp = try await api.consumeOperatorMagicLink(token: token)
        try Keychain.write(
            resp.session_token, account: Keychain.operatorAccount,
        )
        api.bearer = resp.session_token
        state = .operatorUser(
            OperatorIdentity(user: resp.user, workspace: resp.workspace),
        )
        UserDefaults.standard.set(
            LastIdentity.operatorUser.rawValue,
            forKey: Self.lastIdentityKey,
        )
    }

    // MARK: sign-out

    /// Sign out the currently active identity. Leaves the dormant
    /// slot intact so the future account-switcher (Phase 30) can
    /// flip back without re-authenticating.
    func signOut() {
        switch state {
        case .endUser:
            Keychain.clear(account: Keychain.endUserAccount)
        case .operatorUser:
            Keychain.clear(account: Keychain.operatorAccount)
        default:
            break
        }
        api.bearer = nil
        state = .signedOut
        UserDefaults.standard.removeObject(forKey: Self.lastIdentityKey)
    }

    // MARK: account deletion

    /// Delete the currently active identity's account on the backend,
    /// then clear local credentials (Apple guideline 5.1.1(v)). Throws
    /// if the network call fails, so the caller can keep the user
    /// signed in and surface the error instead of silently wiping
    /// local state while the account still exists server-side.
    func deleteAccount() async throws {
        switch state {
        case .endUser:
            try await api.deleteEndUserAccount()
            Keychain.clear(account: Keychain.endUserAccount)
        case .operatorUser:
            try await api.deleteOperatorAccount()
            Keychain.clear(account: Keychain.operatorAccount)
        default:
            return
        }
        api.bearer = nil
        state = .signedOut
        UserDefaults.standard.removeObject(forKey: Self.lastIdentityKey)
    }

    var client: APIClient { api }
}
