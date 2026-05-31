// Phase 30.4.c: operator-side Runs list.
//
// Read-only paginated list of recent runs in the active workspace.
// Lives behind the Channels|Runs segmented control that 30.4.e wires
// into SlackShellView's main column. Per-row signal is:
//
//   agent name + status badge   what + did it work?
//   relative time               "2m ago" — the at-a-glance read
//   latency / tokens            quick perf glance, secondary
//
// Status derives from the with_summary fields shipped in 30.4.a:
//
//   .denied      denial != nil   policy blocked the run
//   .running     ended_at nil    bot is mid-execution
//   .empty       event_count==0  no LLM call happened (silent run)
//   .ok          everything else completed normally
//
// Pull-to-refresh re-hits /runs?with_summary=true. Detail navigation
// (tap a row → OperatorRunDetailView) lands in 30.4.d + .e.

import SwiftUI

struct OperatorRunsListView: View {
    @EnvironmentObject var auth: AuthStore
    let workspaceID: String

    @State private var runs: [OperatorRun] = []
    @State private var loading: Bool = true
    @State private var loadError: String?

    var body: some View {
        Group {
            if loading && runs.isEmpty {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError, runs.isEmpty {
                errorState(loadError)
            } else if runs.isEmpty {
                emptyState
            } else {
                list
            }
        }
        .task { await load() }
        .refreshable { await load() }
        // Phase 30.4.d: list + detail are a self-contained pair.
        // 30.4.e mounts the list inside SlackShellView's NavigationStack;
        // this destination registration travels with it.
        .navigationDestination(for: RunsNavValue.self) { value in
            switch value {
            case .runDetail(let runID):
                OperatorRunDetailView(runID: runID)
            }
        }
    }

    private var list: some View {
        List {
            ForEach(runs) { run in
                NavigationLink(value: RunsNavValue.runDetail(runID: run.id)) {
                    row(run)
                }
            }
        }
        .listStyle(.plain)
    }

    private func row(_ r: OperatorRun) -> some View {
        let status = runStatus(r)
        return HStack(alignment: .top, spacing: 10) {
            statusDot(status)
                .padding(.top, 6)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(r.agent_name)
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.primary)
                    if let kind = r.trigger_kind {
                        Text(kind)
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color(.tertiarySystemBackground))
                            .clipShape(Capsule())
                    }
                    Spacer()
                    Text(relativeTime(r.started_at))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if let detail = subtitle(r, status: status) {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func statusDot(_ s: RunStatus) -> some View {
        Circle()
            .fill(s.color)
            .frame(width: 8, height: 8)
    }

    private func subtitle(_ r: OperatorRun, status: RunStatus) -> String? {
        switch status {
        case .denied:
            // Show the denial reason: most actionable signal.
            return r.denial?.reason ?? "denied"
        case .running:
            return "running…"
        case .empty:
            return "no LLM calls"
        case .ok:
            var parts: [String] = []
            if let ms = r.latency_ms, ms > 0 {
                parts.append("\(ms)ms")
            }
            if let tokens = r.input_tokens.map({ $0 + (r.output_tokens ?? 0) }),
               tokens > 0 {
                parts.append("\(tokens) tok")
            }
            if let m = r.model {
                parts.append(m)
            }
            return parts.isEmpty ? nil : parts.joined(separator: " · ")
        }
    }

    // MARK: empty + error states

    private var emptyState: some View {
        VStack(spacing: 8) {
            Image(systemName: "tray")
                .font(.system(size: 30))
                .foregroundStyle(.secondary)
            Text("No runs yet")
                .font(.system(size: 15, weight: .medium))
            Text("Bots in this workspace haven't run anything yet.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load runs")
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
            runs = try await auth.client.fetchOperatorRuns()
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
        loading = false
    }
}

// MARK: status

private enum RunStatus {
    case ok, denied, running, empty

    var color: Color {
        switch self {
        case .ok: return .green
        case .denied: return .red
        case .running: return .blue
        case .empty: return Color(.tertiaryLabel)
        }
    }
}

private func runStatus(_ r: OperatorRun) -> RunStatus {
    if r.denied == true { return .denied }
    if r.ended_at == nil { return .running }
    if (r.event_count ?? 0) == 0 { return .empty }
    return .ok
}

// MARK: relative time

private func relativeTime(_ d: Date) -> String {
    let secs = Int(Date().timeIntervalSince(d))
    if secs < 60 { return "\(secs)s ago" }
    if secs < 3600 { return "\(secs / 60)m ago" }
    if secs < 86400 { return "\(secs / 3600)h ago" }
    return "\(secs / 86400)d ago"
}

// MARK: navigation value

/// Hashable value pushed onto the parent NavigationStack when a row
/// is tapped. The SlackShellView's nav destination resolves it (wired
/// in 30.4.e). Lives at module scope so 30.4.d's OperatorRunDetailView
/// can reuse it.
enum RunsNavValue: Hashable {
    case runDetail(runID: String)
}
