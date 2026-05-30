// Phase 30.2: operator-side threads API client.
//
// Threads are how operators chat with their own deployed bots. Posting
// a user message creates the user row plus a 'pending' assistant row
// that the deployed agent claims via POST /agents/{name}/threads/claim
// and fills in. The chat view polls GET /threads/{id} to see the
// assistant row flip from pending to completed.
//
// Field names match _serialize_thread / _serialize_thread_message in
// backend/main.py so the JSON shapes line up.

import Foundation

struct OperatorThread: Codable, Identifiable, Equatable, Hashable {
    let id: String
    let agent_name: String
    let title: String
    let created_at: Date
    let updated_at: Date
}

struct OperatorThreadMessage: Codable, Identifiable, Equatable, Hashable {
    let id: String
    let thread_id: String
    let role: String          // "user" | "assistant"
    let content: String
    let status: String        // "pending" | "completed" | "error"
    let error: String?
    let created_at: Date
    let completed_at: Date?
}

struct OperatorThreadListResponse: Codable {
    let threads: [OperatorThread]
}

struct OperatorThreadDetailResponse: Codable {
    let thread: OperatorThread
    let messages: [OperatorThreadMessage]
}

struct OperatorPostThreadMessageResponse: Codable {
    let user_message: OperatorThreadMessage
    let pending_message: OperatorThreadMessage
}

private struct ThreadCreateBody: Encodable {
    let title: String?
}

private struct ThreadMessageBody: Encodable {
    let content: String
}

extension APIClient {
    func createOperatorThread(
        agentName: String, title: String? = nil,
    ) async throws -> OperatorThread {
        try await request(
            "agents/\(agentName)/threads",
            method: "POST",
            body: ThreadCreateBody(title: title),
        )
    }

    func listOperatorThreads(
        agentName: String,
    ) async throws -> [OperatorThread] {
        let resp: OperatorThreadListResponse = try await request(
            "agents/\(agentName)/threads",
        )
        return resp.threads
    }

    func fetchOperatorThread(
        id: String,
    ) async throws -> OperatorThreadDetailResponse {
        try await request("threads/\(id)")
    }

    func postOperatorThreadMessage(
        threadID: String, content: String,
    ) async throws -> OperatorPostThreadMessageResponse {
        try await request(
            "threads/\(threadID)/messages",
            method: "POST",
            body: ThreadMessageBody(content: content),
        )
    }
}
