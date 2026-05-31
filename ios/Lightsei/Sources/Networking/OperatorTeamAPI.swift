// Phase 30.3.d: operator-side team-conversation API client.
//
// Team conversations are 1:N: one operator message goes through the
// Polaris router (backend/team_router.py) which picks the responding
// subset; each picked agent's claim loop fills its own pending row.
//
// Mirrors OperatorThreadsAPI.swift but for the workspace-team surface
// shipped in Phase 30.3.c. The view polls GET /team-conversations/{id}
// while any pending assistant row exists; each pending row flips to
// completed (or error) when its agent's deployed code claims +
// completes it via the SDK helpers shipped in 30.3.f.
//
// Field names match _serialize_team_conversation /
// _serialize_team_message in backend/main.py.

import Foundation

struct OperatorTeamConversation: Codable, Identifiable, Equatable, Hashable {
    let id: String
    let workspace_id: String
    let title: String
    let created_at: Date
    let updated_at: Date
}

// routed_agents shape on router rows: {"agents": [{name, reason}, ...]}.
// Modeled here so the chat view can render the per-bot reasons under
// the router row.
struct OperatorTeamRoutedAgent: Codable, Equatable, Hashable {
    let name: String
    let reason: String
}

struct OperatorTeamRoutedAgents: Codable, Equatable, Hashable {
    let agents: [OperatorTeamRoutedAgent]
}

struct OperatorTeamMessage: Codable, Identifiable, Equatable, Hashable {
    let id: String
    let conversation_id: String
    // "user" | "router" | "assistant"
    let role: String
    let content: String
    // "pending" | "in_progress" | "completed" | "error"
    let status: String
    // Set on assistant rows for attribution; null on user + router rows.
    let agent_name: String?
    // Set only on router rows.
    let routed_agents: OperatorTeamRoutedAgents?
    let error: String?
    let created_at: Date
    let completed_at: Date?
}

struct OperatorTeamConversationListResponse: Codable {
    let conversations: [OperatorTeamConversation]
}

struct OperatorTeamConversationDetailResponse: Codable {
    let conversation: OperatorTeamConversation
    let messages: [OperatorTeamMessage]
}

struct OperatorPostTeamMessageResponse: Codable {
    let user_message: OperatorTeamMessage
    let router_message: OperatorTeamMessage
    // Empty list when the router decided nobody should answer (legal)
    // OR when the router itself errored (the router_message carries
    // the failure in that case; status='error').
    let pending_messages: [OperatorTeamMessage]
}

private struct TeamConversationCreateBody: Encodable {
    let title: String?
}

private struct TeamMessageBody: Encodable {
    let content: String
}

extension APIClient {
    func createTeamConversation(
        title: String? = nil,
    ) async throws -> OperatorTeamConversation {
        try await request(
            "workspaces/me/team-conversations",
            method: "POST",
            body: TeamConversationCreateBody(title: title),
        )
    }

    func listTeamConversations(
    ) async throws -> [OperatorTeamConversation] {
        let resp: OperatorTeamConversationListResponse = try await request(
            "workspaces/me/team-conversations",
        )
        return resp.conversations
    }

    func fetchTeamConversation(
        id: String,
    ) async throws -> OperatorTeamConversationDetailResponse {
        try await request("team-conversations/\(id)")
    }

    func postTeamMessage(
        conversationID: String, content: String,
    ) async throws -> OperatorPostTeamMessageResponse {
        try await request(
            "team-conversations/\(conversationID)/messages",
            method: "POST",
            body: TeamMessageBody(content: content),
        )
    }
}
