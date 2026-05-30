// Phase 30.2: operator (business owner) auth + workspace/agent reads.
//
// Operators authenticate with email + password (POST /auth/login) —
// chosen over magic-link for the app MVP since it needs no Resend /
// deep-link round-trip. The returned bks_ session token is the same
// shape as the end-user one but scoped to an operator User + an
// active workspace.

import Foundation

struct OperatorUser: Codable, Equatable {
    let id: String
    let email: String
    let workspace_id: String
}

struct OperatorWorkspace: Codable, Equatable {
    let id: String
    let name: String
}

struct OperatorLoginRequest: Encodable {
    let email: String
    let password: String
}

struct OperatorLoginResponse: Codable {
    let user: OperatorUser
    let workspace: OperatorWorkspace?
    let session_token: String
}

// One row of GET /me/workspaces. Extra fields (role, plan_tier, ...)
// are ignored by the decoder.
struct OperatorMembership: Codable, Identifiable, Equatable {
    let id: String     // workspace_id
    let name: String
    let is_active: Bool
}

struct OperatorMembershipsResponse: Codable {
    let workspaces: [OperatorMembership]
}

// One row of GET /agents. Only the name is needed for the channel
// list; the rest of the agent row is ignored.
struct OperatorAgent: Codable, Identifiable, Equatable {
    let name: String
    var id: String { name }
}

struct OperatorAgentsResponse: Codable {
    let agents: [OperatorAgent]
}

// GET /auth/me shape. Used on launch to confirm a stored operator
// token still works + recover the active workspace + user.
struct OperatorAuthMeResponse: Codable {
    let user: OperatorUser?
    let workspace: OperatorWorkspace?
    let credential: String?
}

extension APIClient {
    func operatorLogin(
        email: String, password: String,
    ) async throws -> OperatorLoginResponse {
        try await request(
            "auth/login",
            method: "POST",
            body: OperatorLoginRequest(email: email, password: password),
        )
    }

    func fetchMyWorkspaces() async throws -> [OperatorMembership] {
        let resp: OperatorMembershipsResponse = try await request("me/workspaces")
        return resp.workspaces
    }

    func switchWorkspace(_ workspaceID: String) async throws {
        struct Empty: Codable {}
        _ = try await request(
            "me/workspaces/\(workspaceID)/switch",
            method: "POST",
        ) as Empty
    }

    func fetchOperatorAgents() async throws -> [OperatorAgent] {
        let resp: OperatorAgentsResponse = try await request("agents")
        return resp.agents
    }

    func fetchOperatorAuthMe() async throws -> OperatorAuthMeResponse {
        try await request("auth/me")
    }
}
