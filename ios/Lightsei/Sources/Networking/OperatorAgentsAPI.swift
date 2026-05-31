// Phase 30.5.a: operator-side Agents API client.
//
// The Agents surface (list + detail) reads from the existing
// GET /agents + GET /agents/{name} endpoints; no backend changes
// needed. Mirrors _serialize_agent in backend/main.py.
//
// NOTE: the existing `OperatorAgent` struct in OperatorAuthAPI.swift
// is intentionally minimal ({name}-only) for the chat-channel
// selector shipped in Phase 30.2. This module adds `OperatorAgentRow`
// for the full agent shape so the channel-list code keeps decoding
// against the leaner type + the new agents surface gets every field
// it needs.

import Foundation

struct OperatorAgentRow: Codable, Identifiable, Equatable, Hashable {
    let name: String
    let description: String?
    // Per-agent daily cost cap in USD; nil = inherit workspace
    // default. Surfaced on the detail view as a secondary signal.
    let daily_cost_cap_usd: Double?
    let system_prompt: String?
    // Per-agent LLM pin. Both nil = whatever the latest
    // llm_call_completed reported (the SDK's auto-patches set this
    // lazily, so freshly-deployed bots show nil here until they run).
    let provider: String?
    let model: String?
    // Cron-style bots (e.g. polaris) read this at tick time. nil =
    // the bot's env default. Reactive bots ignore it.
    let tick_interval_s: Int?
    // Phase 16 trust-zone ladder: "public" / "internal" / "pii" /
    // "secret". String rather than enum so a new tier doesn't force
    // a client rebuild; the detail view renders unknown values with
    // a neutral pill.
    let sensitivity_level: String
    // Phase 16.2: capability allow-list (default-deny). Empty list =
    // bot can't perform any gated SDK op.
    let capabilities: [String]
    // Phase 16.4: opt-in for cross-zone dispatch.
    let dispatches_cross_zone: Bool
    let created_at: Date
    let updated_at: Date

    // Identifiable via name (which is the primary key in the agents
    // table alongside workspace_id; agent_name is unique inside a
    // workspace).
    var id: String { name }
}

struct OperatorAgentRowsResponse: Codable {
    let agents: [OperatorAgentRow]
}

extension APIClient {
    /// List all non-system agents in the active workspace, sorted
    /// alphabetically (the backend already drops `lightsei.*` rows).
    func fetchAgentRows() async throws -> [OperatorAgentRow] {
        let resp: OperatorAgentRowsResponse = try await request(
            "agents",
        )
        return resp.agents
    }

    func fetchAgentRow(
        name: String,
    ) async throws -> OperatorAgentRow {
        try await request("agents/\(name)")
    }
}

/// Navigation token pushed onto the parent NavigationStack when a
/// row is tapped in OperatorAgentsListView. Module-scoped so the
/// 30.5.c detail view can be the destination resolver's target.
enum AgentsNavValue: Hashable {
    case agentDetail(name: String)
}
