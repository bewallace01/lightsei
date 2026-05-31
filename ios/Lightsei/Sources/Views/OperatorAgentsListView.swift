// Phase 30.5.b: operator-side Agents list.
//
// Read-only list of agents in the active workspace. Lives behind the
// `.agents` slot of the Channels|Runs|Agents segmented control that
// 30.5.d wires into SlackShellView's main column.
//
// Per-row signal:
//
//   name (bold) + zone pill        what bot + how sensitive
//   description (single line)      so the operator can scan without
//                                  opening the detail
//   capability count (right edge)  density signal — "this bot can
//                                  do 5 things" — full chip list
//                                  lands in the 30.5.c detail
//
// Pull-to-refresh re-hits GET /agents. NavigationLink rows push
// AgentsNavValue.agentDetail(name:); destination registration lands
// in 30.5.c alongside the detail view (same pattern as 30.4.c→.d).

import SwiftUI

struct OperatorAgentsListView: View {
    @EnvironmentObject var auth: AuthStore
    let workspaceID: String

    @State private var agents: [OperatorAgentRow] = []
    @State private var loading: Bool = true
    @State private var loadError: String?

    var body: some View {
        Group {
            if loading && agents.isEmpty {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError, agents.isEmpty {
                errorState(loadError)
            } else if agents.isEmpty {
                emptyState
            } else {
                list
            }
        }
        .task { await load() }
        .refreshable { await load() }
        // Phase 30.5.c: list + detail are a self-contained pair.
        // 30.5.d mounts the list inside SlackShellView's
        // NavigationStack and this destination travels with it.
        .navigationDestination(for: AgentsNavValue.self) { value in
            switch value {
            case .agentDetail(let name):
                OperatorAgentDetailView(agentName: name)
            }
        }
    }

    private var list: some View {
        List {
            ForEach(agents) { agent in
                NavigationLink(
                    value: AgentsNavValue.agentDetail(name: agent.name),
                ) {
                    row(agent)
                }
            }
        }
        .listStyle(.plain)
    }

    private func row(_ a: OperatorAgentRow) -> some View {
        HStack(alignment: .top, spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(a.name)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.primary)
                    zonePill(a.sensitivity_level)
                    Spacer()
                    if !a.capabilities.isEmpty {
                        Text("\(a.capabilities.count) cap\(a.capabilities.count == 1 ? "" : "s")")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                    }
                }
                if let desc = a.description?
                    .trimmingCharacters(in: .whitespacesAndNewlines),
                   !desc.isEmpty {
                    Text(desc)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                } else {
                    Text("no description")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                        .italic()
                }
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func zonePill(_ level: String) -> some View {
        let (bg, fg) = zoneColors(level)
        Text(level)
            .font(.system(size: 10, weight: .medium))
            .foregroundStyle(fg)
            .padding(.horizontal, 6)
            .padding(.vertical, 1)
            .background(bg)
            .clipShape(Capsule())
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "person.crop.circle.badge.questionmark")
                .font(.system(size: 30))
                .foregroundStyle(.secondary)
            Text("No agents yet")
                .font(.system(size: 15, weight: .medium))
            Text("Deploy a bot from the web dashboard to see it here.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load agents")
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
}

// Zone color scheme. Lives at file scope so the 30.5.c detail view
// can reuse it for its larger zone badge without duplicating the
// palette.
func zoneColors(_ level: String) -> (bg: Color, fg: Color) {
    switch level {
    case "public":
        return (Color.gray.opacity(0.15), .secondary)
    case "internal":
        return (Color.blue.opacity(0.15), .blue)
    case "pii":
        return (Color.orange.opacity(0.2), .orange)
    case "secret":
        return (Color.red.opacity(0.15), .red)
    default:
        return (Color.gray.opacity(0.15), .secondary)
    }
}
