// Phase 30.6.b: operator-side Cost view.
//
// Single screen, no detail navigation. Three sections:
//
//   Header        MTD big number, projected-EOM subtitle, optional
//                 budget bar. Budget bar only renders when the
//                 workspace has set a monthly cap (budget_usd_monthly);
//                 absent that, the row collapses entirely rather
//                 than showing an empty pill.
//   By agent      table of agent_name + run_count + mtd_usd, sorted
//                 by mtd_usd desc (the backend already does this).
//   By model      table of model + calls + mtd_usd, same sort.
//
// Pull-to-refresh re-hits GET /workspaces/me/cost. As-of timestamp
// renders at the bottom so the operator knows whether they're
// reading a 30-second-old vs a 30-minute-old number.

import SwiftUI

struct OperatorCostView: View {
    @EnvironmentObject var auth: AuthStore
    let workspaceID: String

    @State private var summary: OperatorCostSummary?
    @State private var loading: Bool = true
    @State private var loadError: String?

    var body: some View {
        Group {
            if loading && summary == nil {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError, summary == nil {
                errorState(loadError)
            } else if let s = summary {
                content(s)
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private func content(_ s: OperatorCostSummary) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                header(s)
                byAgentSection(s)
                byModelSection(s)
                asOfFooter(s)
            }
            .padding(16)
        }
    }

    // MARK: header

    private func header(_ s: OperatorCostSummary) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Month to date")
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.5)
            Text(currency(s.mtd_usd))
                .font(.system(size: 34, weight: .semibold))
                .foregroundStyle(.primary)
            HStack(spacing: 6) {
                Text("projected \(currency(s.projected_eom_usd)) by month-end")
                Text("·")
                Text("\(s.run_count) run\(s.run_count == 1 ? "" : "s")")
            }
            .font(.caption)
            .foregroundStyle(.secondary)

            if let cap = s.budget_usd_monthly {
                budgetBar(used: s.mtd_usd, cap: cap, pct: s.budget_used_pct)
                    .padding(.top, 6)
            }
        }
    }

    private func budgetBar(
        used: Double, cap: Double, pct: Double?,
    ) -> some View {
        let fraction = min(max(used / max(cap, 0.0001), 0), 1)
        let color = budgetColor(pct: pct ?? (fraction * 100))
        return VStack(alignment: .leading, spacing: 4) {
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 3)
                        .fill(Color(.tertiarySystemBackground))
                    RoundedRectangle(cornerRadius: 3)
                        .fill(color)
                        .frame(width: geo.size.width * CGFloat(fraction))
                }
            }
            .frame(height: 6)
            HStack(spacing: 4) {
                Text("\(currency(used)) of \(currency(cap))")
                if let pct {
                    Text("·")
                    Text("\(Int(round(pct)))%")
                        .foregroundStyle(color)
                }
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
    }

    private func budgetColor(pct: Double) -> Color {
        if pct >= 90 { return .red }
        if pct >= 70 { return .orange }
        return .green
    }

    // MARK: by-agent

    @ViewBuilder
    private func byAgentSection(_ s: OperatorCostSummary) -> some View {
        section("By agent") {
            if s.by_agent.isEmpty {
                Text("No spend recorded this month.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                VStack(spacing: 0) {
                    ForEach(s.by_agent) { row in
                        agentRow(row)
                        if row.id != s.by_agent.last?.id {
                            Divider()
                        }
                    }
                }
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private func agentRow(_ a: CostByAgentRow) -> some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(a.agent_name)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.primary)
                Text("\(a.run_count) run\(a.run_count == 1 ? "" : "s")")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text(currency(a.mtd_usd))
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(.primary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    // MARK: by-model

    @ViewBuilder
    private func byModelSection(_ s: OperatorCostSummary) -> some View {
        section("By model") {
            if s.by_model.isEmpty {
                Text("No model spend recorded this month.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                VStack(spacing: 0) {
                    ForEach(s.by_model) { row in
                        modelRow(row)
                        if row.id != s.by_model.last?.id {
                            Divider()
                        }
                    }
                }
                .background(Color(.secondarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 10))
            }
        }
    }

    private func modelRow(_ m: CostByModelRow) -> some View {
        HStack(alignment: .center, spacing: 8) {
            VStack(alignment: .leading, spacing: 2) {
                Text(m.model)
                    .font(.system(size: 14, weight: .medium))
                    .foregroundStyle(.primary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Text("\(m.calls) call\(m.calls == 1 ? "" : "s") · \(tokensLabel(m.input_tokens + m.output_tokens))")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            Text(currency(m.mtd_usd))
                .font(.system(size: 14, weight: .medium))
                .foregroundStyle(.primary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private func asOfFooter(_ s: OperatorCostSummary) -> some View {
        Text("as of \(relativeAsOf(s.as_of))")
            .font(.caption2)
            .foregroundStyle(.secondary)
            .padding(.top, 4)
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
            Text("Couldn't load cost")
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
            summary = try await auth.client.fetchCostSummary()
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
        loading = false
    }
}

// MARK: helpers

private func currency(_ v: Double) -> String {
    // Mirror the web /cost rounding: sub-cent shown to 4 places
    // (so a $0.0023 prompt-cache savings number doesn't read $0.00),
    // ≥ $0.01 shown to standard 2 places.
    if v > 0 && v < 0.01 {
        return String(format: "$%.4f", v)
    }
    return String(format: "$%.2f", v)
}

private func tokensLabel(_ n: Int) -> String {
    if n >= 1_000_000 {
        return String(format: "%.1fM tok", Double(n) / 1_000_000)
    }
    if n >= 1_000 {
        return String(format: "%.1fK tok", Double(n) / 1_000)
    }
    return "\(n) tok"
}

private func relativeAsOf(_ d: Date) -> String {
    let secs = Int(Date().timeIntervalSince(d))
    if secs < 60 { return "just now" }
    if secs < 3600 { return "\(secs / 60)m ago" }
    if secs < 86400 { return "\(secs / 3600)h ago" }
    return "\(secs / 86400)d ago"
}
