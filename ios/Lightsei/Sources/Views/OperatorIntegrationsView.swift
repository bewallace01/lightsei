// Phase 30.8.b: operator-side Integrations view.
//
// Single screen, two sections. Fans connectors + Slack reads out in
// parallel via async-let since they're independent endpoints.
//
//   Connectors           one row per registered connector spec.
//                        Installed rows render "Connected as email"
//                        + N scopes; uninstalled rows render
//                        "Not connected" + the summary.
//   Slack                one row per active install (revoked rows
//                        are filtered server-side by default).
//   Footer               "Install integrations from the web at
//                        app.lightsei.com" — OAuth on mobile is
//                        fragile, the web is the right surface to
//                        start a connection. This view is read-only.

import SwiftUI

struct OperatorIntegrationsView: View {
    @EnvironmentObject var auth: AuthStore
    let workspaceID: String

    @State private var connectors: [OperatorConnectorSpec] = []
    @State private var slacks: [OperatorSlackWorkspace] = []
    @State private var loading: Bool = true
    @State private var loadError: String?

    var body: some View {
        Group {
            if loading && connectors.isEmpty && slacks.isEmpty {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError,
                      connectors.isEmpty, slacks.isEmpty {
                errorState(loadError)
            } else {
                content
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                connectorsSection
                slackSection
                installFooter
            }
            .padding(16)
        }
    }

    // MARK: connectors

    @ViewBuilder
    private var connectorsSection: some View {
        section("Connectors") {
            if connectors.isEmpty {
                Text("No connectors registered.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                VStack(spacing: 0) {
                    ForEach(connectors) { c in
                        connectorRow(c)
                        if c.id != connectors.last?.id {
                            Divider().padding(.leading, 12)
                        }
                    }
                }
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private func connectorRow(_ c: OperatorConnectorSpec) -> some View {
        let installed = c.install != nil && c.install?.revoked_at == nil
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(c.display_label)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.primary)
                statusBadge(installed: installed)
                Spacer()
            }
            if let install = c.install, install.revoked_at == nil {
                if let email = install.external_account_email {
                    Text("Connected as \(email)")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Text("\(install.scopes.count) scope\(install.scopes.count == 1 ? "" : "s") granted")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else {
                Text(c.summary)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func statusBadge(installed: Bool) -> some View {
        Text(installed ? "Connected" : "Not connected")
            .font(.system(size: 10, weight: .semibold))
            .foregroundStyle(installed ? .green : .secondary)
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(
                installed
                    ? Color.green.opacity(0.12)
                    : Color(.tertiarySystemBackground),
            )
            .clipShape(Capsule())
    }

    // MARK: slack

    @ViewBuilder
    private var slackSection: some View {
        section("Slack") {
            if slacks.isEmpty {
                Text("No Slack workspace connected.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                VStack(spacing: 0) {
                    ForEach(slacks) { s in
                        slackRow(s)
                        if s.id != slacks.last?.id {
                            Divider().padding(.leading, 12)
                        }
                    }
                }
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private func slackRow(_ s: OperatorSlackWorkspace) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(s.team_name)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.primary)
                statusBadge(installed: s.revoked_at == nil)
                Spacer()
            }
            Text("Installed \(absoluteShortDate(s.installed_at))")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: footer

    private var installFooter: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Image(systemName: "info.circle")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                Text("Install integrations from the web")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
            }
            Text("Connecting a new integration needs an OAuth flow that works best in a desktop browser. Open app.lightsei.com to install or revoke; this view refreshes automatically when you come back.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(.top, 8)
    }

    // MARK: scaffolding

    private func section<C: View>(
        _ title: String,
        @ViewBuilder _ body: () -> C,
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.5)
            body()
        }
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load integrations")
                .font(.system(size: 15, weight: .medium))
            Text(msg)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Retry") { Task { await load() } }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: data

    private func load() async {
        loading = true
        loadError = nil
        do {
            try await auth.client.switchWorkspace(workspaceID)
            // Fan out: both reads are independent.
            async let connectorsFetch = auth.client.fetchConnectorSpecs()
            async let slacksFetch = auth.client.fetchSlackWorkspaces()
            connectors = try await connectorsFetch
            slacks = try await slacksFetch
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
        loading = false
    }
}

// MARK: helpers

private func absoluteShortDate(_ d: Date) -> String {
    let f = DateFormatter()
    f.dateStyle = .medium
    f.timeStyle = .none
    return f.string(from: d)
}
