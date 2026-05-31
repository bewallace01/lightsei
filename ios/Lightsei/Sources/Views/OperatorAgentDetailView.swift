// Phase 30.5.c: operator-side agent detail view.
//
// Tapping a row in OperatorAgentsListView pushes
// AgentsNavValue.agentDetail onto the parent NavigationStack; the
// destination resolver is registered on the list view itself (added
// here in 30.5.c) so list + detail travel as a self-contained pair.
//
// Surface (READ-ONLY this iteration — editing zone/capabilities/
// system_prompt is the natural 30.5.x follow-up):
//
//   Header                name + zone pill (larger than the list's)
//   Description           full text, never truncated
//   Capabilities          chips. Empty list = explicit "(none)"
//                         italic so default-deny is visible, not
//                         confused with "we forgot to render this"
//   Configuration         model · provider · tick interval ·
//                         daily cost cap · dispatches_cross_zone
//                         (only fields that are non-null render)
//   System prompt         monospaced block, collapsed by default
//                         (Slack-style "show more" toggle to keep
//                         the scroll cheap on huge prompts)
//   Timestamps            created · last updated, footer-style
//
// Re-fetches via GET /agents/{name} so the detail can deep-link
// without needing the list-row passed in.

import SwiftUI

struct OperatorAgentDetailView: View {
    @EnvironmentObject var auth: AuthStore
    let agentName: String

    @State private var agent: OperatorAgentRow?
    @State private var loading: Bool = true
    @State private var loadError: String?
    @State private var promptExpanded: Bool = false

    var body: some View {
        Group {
            if loading {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError {
                errorState(loadError)
            } else if let agent {
                content(agent)
            } else {
                emptyState
            }
        }
        .navigationTitle(agentName)
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    private func content(_ a: OperatorAgentRow) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                header(a)
                descriptionBlock(a)
                capabilitiesBlock(a)
                configBlock(a)
                systemPromptBlock(a)
                timestamps(a)
            }
            .padding(16)
        }
    }

    // MARK: header

    private func header(_ a: OperatorAgentRow) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(a.name)
                .font(.system(size: 22, weight: .semibold))
            largeZonePill(a.sensitivity_level)
        }
    }

    @ViewBuilder
    private func largeZonePill(_ level: String) -> some View {
        let (bg, fg) = zoneColors(level)
        Text(level.uppercased())
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(fg)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(bg)
            .clipShape(Capsule())
    }

    // MARK: sections

    @ViewBuilder
    private func descriptionBlock(_ a: OperatorAgentRow) -> some View {
        if let desc = a.description?
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !desc.isEmpty {
            section("Description") {
                Text(desc)
                    .font(.system(size: 14))
                    .foregroundStyle(.primary)
                    .fixedSize(
                        horizontal: false, vertical: true,
                    )
            }
        }
    }

    private func capabilitiesBlock(_ a: OperatorAgentRow) -> some View {
        section("Capabilities") {
            if a.capabilities.isEmpty {
                Text("(none — default-deny)")
                    .font(.system(size: 13))
                    .foregroundStyle(.secondary)
                    .italic()
            } else {
                WrapHStack(items: a.capabilities) { cap in
                    Text(cap)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Color(.tertiarySystemBackground))
                        .clipShape(Capsule())
                }
            }
        }
    }

    @ViewBuilder
    private func configBlock(_ a: OperatorAgentRow) -> some View {
        let rows = configRows(a)
        if !rows.isEmpty {
            section("Configuration") {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(rows, id: \.0) { (label, value) in
                        HStack(alignment: .top, spacing: 8) {
                            Text(label)
                                .font(.system(
                                    size: 12, weight: .medium,
                                ))
                                .foregroundStyle(.secondary)
                                .frame(width: 110, alignment: .leading)
                            Text(value)
                                .font(.system(size: 12))
                                .foregroundStyle(.primary)
                            Spacer()
                        }
                    }
                }
            }
        }
    }

    private func configRows(
        _ a: OperatorAgentRow,
    ) -> [(String, String)] {
        var rows: [(String, String)] = []
        if let m = a.model { rows.append(("Model", m)) }
        if let p = a.provider { rows.append(("Provider", p)) }
        if let ti = a.tick_interval_s {
            rows.append(("Tick interval", "\(ti)s"))
        }
        if let cap = a.daily_cost_cap_usd {
            rows.append(("Daily cap", String(
                format: "$%.2f", cap,
            )))
        }
        rows.append((
            "Cross-zone",
            a.dispatches_cross_zone ? "Allowed" : "Same zone only",
        ))
        return rows
    }

    @ViewBuilder
    private func systemPromptBlock(_ a: OperatorAgentRow) -> some View {
        if let prompt = a.system_prompt?
            .trimmingCharacters(in: .whitespacesAndNewlines),
           !prompt.isEmpty {
            section("System prompt") {
                VStack(alignment: .leading, spacing: 6) {
                    Text(prompt)
                        .font(.system(
                            size: 12, design: .monospaced,
                        ))
                        .foregroundStyle(.primary)
                        .lineLimit(promptExpanded ? nil : 6)
                        .fixedSize(
                            horizontal: false, vertical: true,
                        )
                        .padding(10)
                        .background(Color(.secondarySystemBackground))
                        .clipShape(RoundedRectangle(cornerRadius: 8))
                    if promptIsLong(prompt) {
                        Button(promptExpanded ? "Show less" : "Show more") {
                            promptExpanded.toggle()
                        }
                        .font(.caption)
                    }
                }
            }
        }
    }

    private func timestamps(_ a: OperatorAgentRow) -> some View {
        HStack(spacing: 8) {
            Text("Created \(absoluteDate(a.created_at))")
            Text("·")
            Text("Updated \(absoluteDate(a.updated_at))")
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
        .padding(.top, 4)
    }

    // MARK: scaffolding

    private func section<C: View>(
        _ title: String,
        @ViewBuilder _ body: () -> C,
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title.uppercased())
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.5)
            body()
        }
    }

    private var emptyState: some View {
        Text("Agent not found")
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load agent")
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
            agent = try await auth.client.fetchAgentRow(
                name: agentName,
            )
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
        loading = false
    }
}

// MARK: helpers

private func promptIsLong(_ s: String) -> Bool {
    // Show the "Show more" toggle when the prompt would obviously
    // clip at the 6-line lineLimit. Cheap heuristic: > 280 chars
    // OR contains > 5 newlines.
    s.count > 280 || s.filter({ $0 == "\n" }).count > 5
}

private func absoluteDate(_ d: Date) -> String {
    let f = DateFormatter()
    f.dateStyle = .medium
    f.timeStyle = .none
    return f.string(from: d)
}

// MARK: tiny flow-layout helper for capability chips

/// Minimal flowing HStack. SwiftUI's built-in HStack doesn't wrap,
/// and pulling in the iOS 16 Layout protocol just for capability
/// chips is overkill. This computes row breaks at layout time.
private struct WrapHStack<T: Hashable, V: View>: View {
    let items: [T]
    let content: (T) -> V

    var body: some View {
        FlowLayoutImpl(
            items: items, spacing: 6, runSpacing: 6,
            content: content,
        )
    }
}

private struct FlowLayoutImpl<
    T: Hashable, V: View
>: View {
    let items: [T]
    let spacing: CGFloat
    let runSpacing: CGFloat
    let content: (T) -> V

    @State private var rowWidths: [CGFloat] = []

    var body: some View {
        GeometryReader { geo in
            self.layout(in: geo.size.width)
        }
        // Reserve a vertical floor so the view participates in a
        // VStack without collapsing to height 0 (GeometryReader
        // defaults that way).
        .frame(minHeight: 28)
    }

    private func layout(in maxWidth: CGFloat) -> some View {
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        // Pre-flight pass for positions; we approximate item widths
        // by rendering them in a hidden ZStack with fixed size and
        // using offset. For chip-sized items, the natural intrinsic
        // size works without measurement.
        return ZStack(alignment: .topLeading) {
            ForEach(items, id: \.self) { item in
                content(item)
                    .alignmentGuide(.leading) { d in
                        if x + d.width > maxWidth {
                            x = 0
                            y -= rowHeight + runSpacing
                            rowHeight = 0
                        }
                        let result = -x
                        if item == items.last {
                            x = 0
                        } else {
                            x += d.width + spacing
                        }
                        return result
                    }
                    .alignmentGuide(.top) { d in
                        let result = y
                        rowHeight = max(rowHeight, d.height)
                        if item == items.last {
                            y = 0
                            rowHeight = 0
                        }
                        return result
                    }
            }
        }
    }
}
