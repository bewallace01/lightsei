// Phase 30.6.a: operator-side Cost API client.
//
// The Cost surface reads from the existing GET /workspaces/me/cost
// endpoint; no backend changes needed. The response is pre-aggregated
// server-side (backend/cost.py workspace_cost_mtd) so the mobile view
// is one fetch + render — no client-side rollup, no N+1.
//
// Field names match workspace_cost_mtd's return shape exactly.

import Foundation

struct CostByAgentRow: Codable, Identifiable, Equatable, Hashable {
    let agent_name: String
    let mtd_usd: Double
    let run_count: Int
    // ISO 8601 string on the wire; null when an agent has runs from
    // a prior month but none in the current MTD window.
    let last_run_at: Date?

    var id: String { agent_name }
}

struct CostByModelRow: Codable, Identifiable, Equatable, Hashable {
    let model: String
    let calls: Int
    let input_tokens: Int
    let output_tokens: Int
    let mtd_usd: Double

    var id: String { model }
}

struct OperatorCostSummary: Codable, Equatable {
    let mtd_usd: Double
    // Naive linear extrapolation of mtd / day_of_month * days_in_month.
    let projected_eom_usd: Double
    let run_count: Int
    let by_agent: [CostByAgentRow]
    let by_model: [CostByModelRow]
    // Both nil when the workspace hasn't set a monthly cap. The view
    // hides the budget bar entirely in that case rather than rendering
    // an empty pill.
    let budget_usd_monthly: Double?
    let budget_used_pct: Double?
    let month_start: Date
    let as_of: Date
}

extension APIClient {
    /// Fetch the workspace's MTD spend + per-agent + per-model
    /// breakdown. Safe to poll every 30s the same way the web /cost
    /// page does (response reads runs.cost_usd directly — no event
    /// scan per call).
    func fetchCostSummary() async throws -> OperatorCostSummary {
        try await request("workspaces/me/cost")
    }
}
