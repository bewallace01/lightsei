// Phase 30.9.a: operator-side Workspace ("me") API client.
//
// GET /workspaces/me returns the full settings-shape workspace
// record. Distinct from the OperatorWorkspace struct in
// OperatorAuthAPI.swift (which is {id, name}-only, the login
// response shape from POST /auth/login). The Settings view (30.9.b)
// reads this for the live workspace state (vendor_slug, plan_tier,
// monthly budget cap, etc) — the login snapshot would go stale on
// anything edited via the web after sign-in.
//
// Mirrors _serialize_workspace() in backend/main.py.

import Foundation

struct OperatorWorkspaceMe: Codable, Equatable, Hashable {
    let id: String
    let name: String
    let created_at: Date
    // NULL when the workspace hasn't set a monthly cap. Same field
    // the Cost view (30.6) already reads via the cost summary
    // response.
    let budget_usd_monthly: Double?
    // Phase 17.7 billing surface. "free" / "pro" / etc — string
    // rather than enum so a new tier doesn't force a client rebuild.
    let plan_tier: String
    let free_credits_remaining_usd: Double
    // True iff the workspace has been through Stripe Checkout. The
    // /account web page uses this to pick between "Subscribe" and
    // "Manage billing" CTAs; iOS will show the same split with a
    // "Manage on web" footer (no Stripe portal in-app this iteration).
    let has_stripe_customer: Bool
    // Phase 21.9: auto-apply Polaris widget-incident-response fixes.
    let polaris_auto_apply_widget_fixes: Bool
    // Phase 26.1: operator-claimed Constellation slug. NULL until
    // claimed via POST /workspaces/me/vendor-slug from the web.
    // Internal identifier is still "vendor_slug" per the
    // memory note on the project-wide rename.
    let vendor_slug: String?
}

extension APIClient {
    /// Fetch the active workspace's full settings-shape record.
    /// Caller is responsible for the workspace switch via
    /// switchWorkspace() if a particular workspace context is
    /// required (the Settings view does so before calling).
    func fetchWorkspaceMe() async throws -> OperatorWorkspaceMe {
        try await request("workspaces/me")
    }
}
