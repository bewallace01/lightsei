// Phase 29.3: vendor list + per-vendor conversation list endpoints.
//
// Mirrors the dashboard's fetchEndUserVendorsWithCounts +
// fetchEndUserVendorConversations helpers, so a backend response
// shape change surfaces as a compile error on both surfaces at once.

import Foundation

struct EndUserVendor: Codable, Identifiable, Equatable, Hashable {
    let id: String                              // workspace_id
    let name: String
    let vendor_slug: String?
    let widget_public_id: String?
    let customer_facing_agent_name: String?
    let unread_count: Int?
    let notification_pref: String?
}

struct VendorsResponse: Codable {
    let vendors: [EndUserVendor]
}

struct EndUserVendorConversation: Codable, Identifiable, Equatable {
    let id: String
    let status: String
    let customer_facing_agent_name: String?
    let started_at: String
    let last_message_at: String
    let resolved_at: String?
}

struct ConversationsResponse: Codable {
    let vendor: EndUserVendor
    let conversations: [EndUserVendorConversation]
}

struct RedeemInviteRequest: Encodable {
    let code: String
}

struct RedeemInviteResponse: Codable {
    let linked: Bool
    let vendor: EndUserVendor?
}

struct VendorPatchRequest: Encodable {
    let notification_pref: String?
    let display_name_override: String?
}

struct VendorPatchResponse: Codable {
    let workspace_id: String
    let notification_pref: String
    let display_name_override: String?
}

struct VendorUnlinkResponse: Codable {
    let unlinked: Bool
    let workspace_id: String
}

extension APIClient {
    func fetchVendors() async throws -> [EndUserVendor] {
        let resp: VendorsResponse = try await request("me/end-user/vendors")
        return resp.vendors
    }

    func fetchConversations(
        vendorSlug slug: String,
    ) async throws -> ConversationsResponse {
        try await request(
            "me/end-user/vendors/\(slug)/conversations",
        )
    }

    func redeemInvite(code: String) async throws -> RedeemInviteResponse {
        try await request(
            "me/end-user/redeem-invite",
            method: "POST",
            body: RedeemInviteRequest(code: code),
        )
    }

    func patchVendorSettings(
        workspaceID: String,
        notificationPref: String? = nil,
        displayName: String? = nil,
    ) async throws -> VendorPatchResponse {
        try await request(
            "me/end-user/vendors/\(workspaceID)",
            method: "PATCH",
            body: VendorPatchRequest(
                notification_pref: notificationPref,
                display_name_override: displayName,
            ),
        )
    }

    func unlinkVendor(
        workspaceID: String,
    ) async throws -> VendorUnlinkResponse {
        try await request(
            "me/end-user/vendors/\(workspaceID)",
            method: "DELETE",
        )
    }
}
