// Phase 30.7.a: operator-side Zones view.
//
// Reads the existing GET /agents response (no backend changes, no
// new API client) and groups by sensitivity_level. The web /zones
// page renders a constellation SVG; on mobile that doesn't pay
// (gesture-tight + redundant with the constellation rail in the
// shell already) so this surface is a flat data view:
//
//   Header           "Trust zones" + agent count chip
//   Zone counts      2x2 grid of zone cards with agent counts. The
//                    cards' color swatches mirror OperatorAgentsListView's
//                    zonePill palette so the same vocabulary travels
//                    between surfaces.
//   Per-zone groups  one card per zone that has agents (empty zones
//                    are hidden — operators don't need to see "0 agents
//                    in secret"; the count card already says that).
//                    Each agent row is a NavigationLink reusing
//                    AgentsNavValue.agentDetail so the existing 30.5.c
//                    detail view opens for free.
//   Cross-zone       callout listing agents with dispatches_cross_zone
//                    == true. This is the audit-risk surface — these
//                    agents can target other zones, so the operator
//                    can spot anomalies at a glance.
//
// Pull-to-refresh re-hits /agents. Detail nav reuses the
// AgentsNavValue resolver that OperatorAgentsListView already
// registers; this view also registers it so the link still resolves
// when the operator is on the Zones tab instead of Agents.

import SwiftUI

struct OperatorZonesView: View {
    @EnvironmentObject var auth: AuthStore
    let workspaceID: String

    @State private var agents: [OperatorAgentRow] = []
    @State private var loading: Bool = true
    @State private var loadError: String?

    // Canonical display order: least → most sensitive. Matches the
    // backend/_VALID_SENSITIVITY_LEVELS ladder.
    private let zoneOrder = ["public", "internal", "pii", "secret"]

    var body: some View {
        Group {
            if loading && agents.isEmpty {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError, agents.isEmpty {
                errorState(loadError)
            } else {
                content
            }
        }
        .task { await load() }
        .refreshable { await load() }
        // Sibling registration of the AgentsNavValue destination so
        // taps on agent rows resolve while this view is on-screen.
        // OperatorAgentsListView (30.5.c) registers the same resolver
        // for the Agents tab.
        .navigationDestination(for: AgentsNavValue.self) { value in
            switch value {
            case .agentDetail(let name):
                OperatorAgentDetailView(agentName: name)
            }
        }
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header
                zoneCountGrid
                ForEach(zoneOrder, id: \.self) { z in
                    zoneGroupSection(zone: z)
                }
                crossZoneSection
            }
            .padding(16)
        }
    }

    // MARK: header

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text("Trust zones")
                .font(.system(size: 22, weight: .semibold))
            Text("\(agents.count) agent\(agents.count == 1 ? "" : "s")")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
    }

    // MARK: zone count cards

    private var zoneCountGrid: some View {
        let cols = [
            GridItem(.flexible(), spacing: 10),
            GridItem(.flexible(), spacing: 10),
        ]
        return LazyVGrid(columns: cols, spacing: 10) {
            ForEach(zoneOrder, id: \.self) { z in
                zoneCountCard(zone: z, count: countInZone(z))
            }
        }
    }

    private func zoneCountCard(zone: String, count: Int) -> some View {
        let (bg, fg) = zoneColors(zone)
        return VStack(alignment: .leading, spacing: 4) {
            Text(zone.uppercased())
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(fg)
                .tracking(0.5)
            Text("\(count)")
                .font(.system(size: 26, weight: .semibold))
                .foregroundStyle(.primary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(bg)
        .clipShape(RoundedRectangle(cornerRadius: 10))
    }

    // MARK: per-zone groups

    @ViewBuilder
    private func zoneGroupSection(zone: String) -> some View {
        let inZone = agentsIn(zone)
        if !inZone.isEmpty {
            section(zone.uppercased()) {
                VStack(spacing: 0) {
                    ForEach(inZone) { agent in
                        NavigationLink(
                            value: AgentsNavValue.agentDetail(
                                name: agent.name,
                            ),
                        ) {
                            agentRow(agent)
                        }
                        .buttonStyle(.plain)
                        if agent.id != inZone.last?.id {
                            Divider().padding(.leading, 12)
                        }
                    }
                }
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private func agentRow(_ a: OperatorAgentRow) -> some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(a.name)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.primary)
                if let desc = a.description?
                    .trimmingCharacters(in: .whitespacesAndNewlines),
                   !desc.isEmpty {
                    Text(desc)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            if a.dispatches_cross_zone {
                // Same warning icon as the cross-zone callout so the
                // visual language is consistent.
                Image(systemName: "arrow.left.arrow.right")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.orange)
            }
            Image(systemName: "chevron.right")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .contentShape(Rectangle())
    }

    // MARK: cross-zone callout

    @ViewBuilder
    private var crossZoneSection: some View {
        let crossZoneAgents = agents.filter { $0.dispatches_cross_zone }
        if !crossZoneAgents.isEmpty {
            section("Cross-zone dispatchers") {
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 6) {
                        Image(systemName: "arrow.left.arrow.right")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.orange)
                        Text(
                            "These agents can dispatch to other "
                            + "trust zones. Review carefully — "
                            + "cross-zone is the audit-risk surface."
                        )
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(
                            horizontal: false, vertical: true,
                        )
                    }
                    VStack(spacing: 0) {
                        ForEach(crossZoneAgents) { agent in
                            NavigationLink(
                                value: AgentsNavValue.agentDetail(
                                    name: agent.name,
                                ),
                            ) {
                                HStack(spacing: 6) {
                                    Text(agent.name)
                                        .font(.system(
                                            size: 14, weight: .medium,
                                        ))
                                        .foregroundStyle(.primary)
                                    Text(agent.sensitivity_level)
                                        .font(.system(
                                            size: 10, weight: .medium,
                                        ))
                                        .foregroundStyle(
                                            zoneColors(
                                                agent.sensitivity_level,
                                            ).fg,
                                        )
                                        .padding(.horizontal, 5)
                                        .padding(.vertical, 1)
                                        .background(
                                            zoneColors(
                                                agent.sensitivity_level,
                                            ).bg,
                                        )
                                        .clipShape(Capsule())
                                    Spacer()
                                    Image(systemName: "chevron.right")
                                        .font(.system(
                                            size: 11, weight: .semibold,
                                        ))
                                        .foregroundStyle(.tertiary)
                                }
                                .padding(.horizontal, 12)
                                .padding(.vertical, 8)
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                            if agent.id != crossZoneAgents.last?.id {
                                Divider().padding(.leading, 12)
                            }
                        }
                    }
                    .background(Color(.secondarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 10))
                }
            }
        }
    }

    // MARK: scaffolding

    private func section<C: View>(
        _ title: String,
        @ViewBuilder _ body: () -> C,
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.5)
            body()
        }
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load zones")
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
            agents = try await auth.client.fetchAgentRows()
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
        loading = false
    }

    private func countInZone(_ zone: String) -> Int {
        agents.filter { $0.sensitivity_level == zone }.count
    }

    private func agentsIn(_ zone: String) -> [OperatorAgentRow] {
        agents.filter { $0.sensitivity_level == zone }
    }
}
