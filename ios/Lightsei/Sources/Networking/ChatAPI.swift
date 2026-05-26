// Phase 29.3: widget thread polling + message send endpoints.
//
// The widget endpoints predate the end-user identity story (Phase
// 21 vs Phase 25) so they're keyed by `widget_public_id` (the
// workspace's public widget id) rather than the workspace UUID.
// The end-user bearer scopes the request to this end_user_id (per
// Phase 25.4 + 27.6) so no extra plumbing is needed here.

import Foundation

struct WidgetMessage: Codable, Identifiable, Equatable {
    let id: Int
    let role: String   // "user" | "bot" | "operator" | "system"
    let text: String
    let sent_at: String
}

struct WidgetThread: Codable {
    let conversation_id: String
    let status: String
    let messages: [WidgetMessage]
}

struct PostMessageRequest: Encodable {
    let text: String
    let conversation_id: String?
}

struct PostMessageResponse: Codable {
    let conversation_id: String
    let message_id: Int
    let job_id: String?
}

extension APIClient {
    func postWidgetMessage(
        publicId: String,
        text: String,
        conversationId: String?,
    ) async throws -> PostMessageResponse {
        try await request(
            "widget/\(publicId)/messages",
            method: "POST",
            body: PostMessageRequest(
                text: text,
                conversation_id: conversationId,
            ),
        )
    }

    /// Fetch a conversation's full thread, or just messages after
    /// `since` (incremental poll). `since` is the highest message
    /// id already on the client.
    func fetchWidgetThread(
        publicId: String,
        conversationId: String,
        since: Int? = nil,
    ) async throws -> WidgetThread {
        var path = "widget/\(publicId)/conversations/\(conversationId)"
        if let since {
            path += "?since=\(since)"
        }
        return try await request(path)
    }
}
