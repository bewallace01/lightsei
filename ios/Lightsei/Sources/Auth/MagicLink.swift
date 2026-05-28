// Phase 29.2a: parse a magic-link URL into its token.
//
// Accepts the production web URL (universal-link path) AND the
// custom-scheme URL we ship today before 29.2b adds
// apple-app-site-association on app.lightsei.com:
//
//   https://app.lightsei.com/c/auth/magic-link?token=XXX  (29.2b)
//   lightsei://auth/magic-link?token=XXX                  (29.2a)
//
// Also accepts a bare token string (for the paste-link fallback
// on the SignInView), matching the web /c page's heuristic.

import Foundation

enum MagicLink {
    /// Extract a magic-link token from a pasted string. Returns
    /// nil if the input doesn't look like a token or a URL with a
    /// `?token=...` query.
    static func extractToken(from raw: String) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return nil }
        if let url = URL(string: trimmed),
           let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
           let token = comps.queryItems?.first(where: { $0.name == "token" })?.value,
           !token.isEmpty {
            return token
        }
        // Heuristic: base64url-ish token, 20+ chars.
        let allowed = CharacterSet(charactersIn:
            "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")
        if trimmed.count >= 20 && trimmed.unicodeScalars.allSatisfy({
            allowed.contains($0)
        }) {
            return trimmed
        }
        return nil
    }

    static func extractVendorInviteCode(from raw: String) -> String? {
        guard let url = URL(string: raw),
              let comps = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let code = comps.queryItems?.first(
                where: { $0.name == "vendor_invite_code" }
              )?.value,
              !code.isEmpty
        else {
            return nil
        }
        return code
    }
}
