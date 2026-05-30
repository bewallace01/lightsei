// Phase 30.1: end-user implementation of ChatDataSource.
//
// Maps the existing end-user data (Constellations + their one
// customer-facing bot) onto the neutral server/channel shapes the
// Slack shell draws. Holds the fetched EndUserVendor rows so the
// shell can hand the original vendor to the existing ChatView when
// a bot channel is opened (ChatView is still vendor-typed in 30.1;
// 30.2 generalizes the chat pane).

import Foundation

@MainActor
final class EndUserChatSource: ChatDataSource {
    private let client: APIClient
    private var vendorsByID: [String: EndUserVendor] = [:]

    init(client: APIClient) {
        self.client = client
    }

    func loadServers() async throws -> [ChatServer] {
        let vendors = try await client.fetchVendors()
        vendorsByID = Dictionary(
            vendors.map { ($0.id, $0) },
            uniquingKeysWith: { first, _ in first },
        )
        return vendors.map {
            ChatServer(id: $0.id, name: $0.name, unread: $0.unread_count ?? 0)
        }
    }

    func loadChannels(for server: ChatServer) async throws -> [ChatChannel] {
        // One customer-facing bot per Constellation on the end-user
        // side, so the channel list is a single bot channel. The
        // multi-bot list arrives with the operator source (30.2).
        guard let v = vendorsByID[server.id] else { return [] }
        let botName = v.customer_facing_agent_name ?? "assistant"
        return [
            ChatChannel(
                id: botName,
                name: botName,
                kind: .bot,
                serverID: server.id,
            ),
        ]
    }

    // Resolve a server back to its EndUserVendor so the shell can
    // open the existing ChatView for the bot channel.
    func vendor(for serverID: String) -> EndUserVendor? {
        vendorsByID[serverID]
    }

    func target(for channel: ChatChannel) -> ChatTarget? {
        guard channel.kind == .bot,
              let v = vendorsByID[channel.serverID] else { return nil }
        return .endUserVendor(v)
    }
}
