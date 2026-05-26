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
}
