// Phase 30.2: operator implementation of ChatDataSource.
//
// Maps an operator's workspace memberships onto the neutral
// server/channel shape the Slack shell renders:
//
//   server   = one membership row (workspace) from GET /me/workspaces
//   channel  = one agent row from GET /agents in the SELECTED workspace
//
// Per-tap side effect: selecting a server flips the operator's active
// workspace on the backend via POST /me/workspaces/{id}/switch so the
// subsequent /agents call scopes correctly. The shell then opens an
// OperatorChatView (threads-based chat) when a bot channel is tapped.
//
// Unread counts aren't surfaced operator-side yet; future iteration
// can wire them from /agents or a dedicated count endpoint.

import Foundation

@MainActor
final class OperatorChatSource: ChatDataSource {
    private let client: APIClient
    private var workspacesByID: [String: OperatorMembership] = [:]

    init(client: APIClient) {
        self.client = client
    }

    func loadServers() async throws -> [ChatServer] {
        let memberships = try await client.fetchMyWorkspaces()
        workspacesByID = Dictionary(
            memberships.map { ($0.id, $0) },
            uniquingKeysWith: { first, _ in first },
        )
        return memberships.map {
            ChatServer(id: $0.id, name: $0.name, unread: 0)
        }
    }

    func loadChannels(for server: ChatServer) async throws -> [ChatChannel] {
        // Operators authenticate against an active workspace, so we
        // switch first then read the agent list. The active workspace
        // sticks for the rest of the session until another tap flips
        // it again.
        try await client.switchWorkspace(server.id)
        let agents = try await client.fetchOperatorAgents()
        return agents.map {
            ChatChannel(
                id: $0.name,
                name: $0.name,
                kind: .bot,
                serverID: server.id,
            )
        }
    }

    func target(for channel: ChatChannel) -> ChatTarget? {
        guard channel.kind == .bot else { return nil }
        return .operatorBot(
            workspaceID: channel.serverID,
            agentName: channel.id,
        )
    }
}
