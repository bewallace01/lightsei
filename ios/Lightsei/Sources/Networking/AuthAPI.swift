// Phase 29.2a: typed wrappers for the end-user auth endpoints.
//
// Mirrors the dashboard's api.ts helpers (requestEndUserMagicLink /
// consumeEndUserMagicLink / fetchEndUserMe) so a backend response
// shape change surfaces as a compile error on both surfaces at
// once. Phase 29.4 adds an APNS device-token register endpoint
// here too.

import Foundation

struct EndUser: Codable, Equatable {
    let id: String
    let email: String
    let display_name: String?
    let email_verified: Bool
    let auth_provider: String
}

struct MagicLinkConsumeResponse: Codable {
    let session_token: String
    let end_user: EndUser
    let is_new_end_user: Bool
}

struct MagicLinkRequestRequest: Encodable {
    let email: String
    let vendor_invite_code: String?
}

struct MagicLinkConsumeRequest: Encodable {
    let token: String
    let vendor_invite_code: String?
}

extension APIClient {
    func requestMagicLink(
        email: String, vendorInviteCode: String? = nil,
    ) async throws {
        struct EmptyResponse: Codable {}
        _ = try await request(
            "auth/end-user/magic-link/request",
            method: "POST",
            body: MagicLinkRequestRequest(
                email: email,
                vendor_invite_code: vendorInviteCode,
            ),
        ) as EmptyResponse
    }

    func consumeMagicLink(
        token: String, vendorInviteCode: String? = nil,
    ) async throws -> MagicLinkConsumeResponse {
        try await request(
            "auth/end-user/magic-link/consume",
            method: "POST",
            body: MagicLinkConsumeRequest(
                token: token,
                vendor_invite_code: vendorInviteCode,
            ),
        )
    }

    func fetchEndUserMe() async throws -> EndUserMeResponse {
        try await request("me/end-user")
    }

    // Phase 31.5.g: in-app account deletion (Apple 5.1.1(v)). Hard
    // delete; the backend cascades sessions, vendor links, push, apns.
    func deleteEndUserAccount() async throws {
        struct EmptyResponse: Codable {}
        _ = try await request("me/end-user", method: "DELETE") as EmptyResponse
    }
}

struct EndUserMeResponse: Codable {
    let end_user: EndUser
    let push_vapid_public_key: String?
    let has_active_push_subscription: Bool?
    // linked_vendors omitted from the iOS surface until 29.3 needs
    // the vendor cards. Decoder ignores extra keys by default.
}
