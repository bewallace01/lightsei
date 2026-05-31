// Phase 30.4.d: operator-side run detail view.
//
// Tapping a row in OperatorRunsListView pushes RunsNavValue.runDetail
// onto the parent NavigationStack. The destination resolver wires to
// this view (registered on the list view itself in 30.4.c so the
// list + detail are a self-contained pair).
//
// Surface:
//
//   Header  agent + ran-at + duration. Pulled from the
//           OperatorRunSnapshot returned alongside the events so
//           the view stands on its own and doesn't need the list
//           row passed in.
//   Events  one card per event, kind + timestamp + pretty-printed
//           payload. Payload uses a monospaced font so JSON keys +
//           values line up. Long payloads truncate by default; tap
//           the card to expand. Empty payloads collapse to just the
//           header line.

import SwiftUI

struct OperatorRunDetailView: View {
    @EnvironmentObject var auth: AuthStore
    let runID: String

    @State private var snapshot: OperatorRunSnapshot?
    @State private var events: [OperatorRunEvent] = []
    @State private var loading: Bool = true
    @State private var loadError: String?
    @State private var expanded: Set<Int> = []

    var body: some View {
        Group {
            if loading {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError {
                errorState(loadError)
            } else {
                content
            }
        }
        .navigationTitle("Run")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        .refreshable { await load() }
    }

    private var content: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                header
                if events.isEmpty {
                    Text("No events recorded for this run.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.top, 8)
                } else {
                    ForEach(events) { ev in
                        eventCard(ev)
                    }
                }
            }
            .padding(16)
        }
    }

    @ViewBuilder
    private var header: some View {
        if let s = snapshot {
            VStack(alignment: .leading, spacing: 4) {
                Text(s.agent_name)
                    .font(.system(size: 19, weight: .semibold))
                HStack(spacing: 6) {
                    Text("ran \(relativeRunTime(s.started_at))")
                    if let dur = durationString(
                        from: s.started_at, to: s.ended_at,
                    ) {
                        Text("·")
                        Text(dur)
                    }
                    if s.ended_at == nil {
                        Text("·")
                        Text("running")
                            .foregroundStyle(.blue)
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
            }
            .padding(.bottom, 4)
        }
    }

    private func eventCard(_ ev: OperatorRunEvent) -> some View {
        let isExpanded = expanded.contains(ev.id)
        let payloadString = ev.payload.prettyPrinted
        let payloadIsEmpty = (
            payloadString == "{}" || payloadString.isEmpty
        )
        return Button {
            if payloadIsEmpty { return }
            if isExpanded { expanded.remove(ev.id) }
            else { expanded.insert(ev.id) }
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Text(ev.kind)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(.primary)
                    Spacer()
                    Text(eventTimeLabel(ev.timestamp))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
                if !payloadIsEmpty {
                    Text(payloadString)
                        .font(.system(size: 11, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(isExpanded ? nil : 4)
                        .truncationMode(.tail)
                        .frame(
                            maxWidth: .infinity, alignment: .leading,
                        )
                }
            }
            .padding(12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color(.secondarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .disabled(payloadIsEmpty)
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load run")
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
            let resp = try await auth.client.fetchOperatorRunWithEvents(
                runID: runID,
            )
            snapshot = resp.run
            events = resp.events
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
        loading = false
    }
}

// MARK: helpers

private func relativeRunTime(_ d: Date) -> String {
    let secs = Int(Date().timeIntervalSince(d))
    if secs < 60 { return "\(secs)s ago" }
    if secs < 3600 { return "\(secs / 60)m ago" }
    if secs < 86400 { return "\(secs / 3600)h ago" }
    return "\(secs / 86400)d ago"
}

private func durationString(
    from start: Date, to end: Date?,
) -> String? {
    guard let end else { return nil }
    let secs = end.timeIntervalSince(start)
    if secs < 0 { return nil }
    if secs < 1 { return "\(Int(secs * 1000))ms" }
    if secs < 60 { return String(format: "%.1fs", secs) }
    let m = Int(secs / 60)
    let s = Int(secs.truncatingRemainder(dividingBy: 60))
    return "\(m)m \(s)s"
}

private func eventTimeLabel(_ d: Date) -> String {
    let f = DateFormatter()
    f.dateFormat = "HH:mm:ss"
    return f.string(from: d)
}
