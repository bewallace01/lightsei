// Phase 30.8.a: operator-side Integrations API client.
//
// The Integrations surface reads two parallel endpoints — no
// backend changes needed:
//
//   GET /workspaces/me/connectors        → registry-shaped list
//                                          (Gmail / Calendar / Drive
//                                          at v1) with per-workspace
//                                          install state
//   GET /workspaces/me/slack/workspaces  → Slack workspaces installed
//                                          to this Lightsei workspace
//
// Field names match _serialize_connector_install + the
// list_workspace_connectors response shape +
// _serialize_slack_workspace exactly.
//
// READ-ONLY in this iteration. OAuth on mobile is fragile (system
// browser handoff, deep-link return, scope sheets in a popover),
// and the web is the right surface to start a new connection. The
// iOS view surfaces "Install from app.lightsei.com" rather than
// trying to drive OAuth from the phone.

import Foundation

// Connectors registry entry. Mirrors the backend dict produced by
// list_workspace_connectors() — one entry per registry spec, with
// `install` nil when the workspace hasn't connected it yet.
struct OperatorConnectorSpec: Codable, Identifiable, Equatable, Hashable {
    let type: String
    let display_label: String
    let oauth_provider: String
    let default_scopes: [String]
    let declared_zones: [String]
    let summary: String
    let install: OperatorConnectorInstall?

    var id: String { type }
}

struct OperatorConnectorInstall: Codable, Equatable, Hashable {
    let id: String
    // Set to the Google account email after a Google-OAuth install
    // (Gmail / Calendar / Drive). Null on connectors that don't have
    // a per-install account identity yet.
    let external_account_email: String?
    let scopes: [String]
    let installed_at: Date
    let installed_by_user_id: String?
    // Set when a connector was disconnected. /me/connectors filters
    // these out by default, so the iOS view treats their presence as
    // a stale-row red flag.
    let revoked_at: Date?
}

struct OperatorConnectorsListResponse: Codable {
    let connectors: [OperatorConnectorSpec]
}

// Slack workspace install. Bot token + signing secret stay encrypted
// in storage and never appear over the wire (mirrors web behavior).
struct OperatorSlackWorkspace: Codable, Identifiable, Equatable, Hashable {
    let slack_team_id: String
    let team_name: String
    let bot_user_id: String?
    let installed_at: Date
    let installed_by_user_id: String?
    let revoked_at: Date?

    var id: String { slack_team_id }
}

struct OperatorSlackWorkspacesResponse: Codable {
    let workspaces: [OperatorSlackWorkspace]
}

extension APIClient {
    /// Fetch the connectors registry + install state. Returns every
    /// registered connector (so cards can render the not-installed
    /// state alongside connected ones).
    func fetchConnectorSpecs() async throws -> [OperatorConnectorSpec] {
        let resp: OperatorConnectorsListResponse = try await request(
            "workspaces/me/connectors",
        )
        return resp.connectors
    }

    /// Fetch only active Slack workspaces (the default; the
    /// include_revoked=true variant is an audit-log surface we
    /// don't need for the iOS list).
    func fetchSlackWorkspaces() async throws -> [OperatorSlackWorkspace] {
        let resp: OperatorSlackWorkspacesResponse = try await request(
            "workspaces/me/slack/workspaces",
        )
        return resp.workspaces
    }
}
