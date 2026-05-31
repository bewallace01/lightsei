// Phase 30.3.d: operator-side team channel chat.
//
// 1:N chat instead of 1:1: one operator message is routed by Polaris
// (backend/team_router.py) which picks a subset of the workspace's
// bots; each picked bot's claim loop fills its own pending row. This
// view renders three message kinds:
//
//   user      → operator's message, right-aligned, accent bubble.
//   router    → "Polaris" row showing the routing summary + per-bot
//               reasons. Rendered as a centered, smaller card.
//   assistant → one bubble per agent the router picked. The
//               agent_name label sits above the bubble so multi-bot
//               replies in the same turn are readable.
//
// MVP scope: one persistent team conversation per (workspace) visit
// (most recent or new), same polling loop as OperatorChatView. If a
// picked bot isn't running, its pending row sits "thinking…" forever
// — that's an operational concern surfaced visually rather than a
// client bug.

import SwiftUI

struct OperatorTeamChatView: View {
    @EnvironmentObject var auth: AuthStore
    let workspaceID: String

    @State private var conversationID: String?
    @State private var messages: [OperatorTeamMessage] = []
    @State private var loading: Bool = true
    @State private var loadError: String?
    @State private var draft: String = ""
    @State private var sending: Bool = false
    @State private var sendError: String?
    @State private var pollTask: Task<Void, Never>?

    var body: some View {
        VStack(spacing: 0) {
            content
            composer
        }
        .navigationTitle("team")
        .navigationBarTitleDisplayMode(.inline)
        .task { await initialLoad() }
        .onDisappear { pollTask?.cancel() }
    }

    @ViewBuilder
    private var content: some View {
        if loading {
            ProgressView()
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let loadError {
            VStack(spacing: 10) {
                Text("Couldn't load")
                    .font(.system(size: 15, weight: .medium))
                Text(loadError)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                Button("Retry") { Task { await initialLoad() } }
            }
            .padding(24)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(messages) { msg in
                            row(msg).id(msg.id)
                        }
                    }
                    .padding(.horizontal, 16)
                    .padding(.vertical, 12)
                }
                .onChange(of: messages.count) { _ in
                    if let last = messages.last {
                        withAnimation(.easeOut(duration: 0.15)) {
                            proxy.scrollTo(last.id, anchor: .bottom)
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private func row(_ m: OperatorTeamMessage) -> some View {
        switch m.role {
        case "user":
            userRow(m)
        case "router":
            routerRow(m)
        case "assistant":
            assistantRow(m)
        default:
            EmptyView()
        }
    }

    private func userRow(_ m: OperatorTeamMessage) -> some View {
        HStack(alignment: .top, spacing: 0) {
            Spacer(minLength: 40)
            Text(m.content)
                .font(.system(size: 15))
                .foregroundStyle(.white)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color.accentColor)
                .clipShape(RoundedRectangle(cornerRadius: 14))
        }
    }

    private func routerRow(_ m: OperatorTeamMessage) -> some View {
        let picks = m.routed_agents?.agents ?? []
        return HStack {
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 6) {
                    Image(systemName: "sparkle")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                    Text("Polaris")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.secondary)
                    if m.status == "error" {
                        Text("error")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(.red)
                    }
                }
                Text(m.content)
                    .font(.system(size: 13))
                    .foregroundStyle(
                        m.status == "error" ? .red : .primary,
                    )
                if !picks.isEmpty {
                    VStack(alignment: .leading, spacing: 2) {
                        ForEach(picks, id: \.name) { p in
                            HStack(alignment: .top, spacing: 6) {
                                Text(p.name)
                                    .font(.system(
                                        size: 11, weight: .semibold,
                                    ))
                                    .foregroundStyle(.secondary)
                                Text(p.reason)
                                    .font(.system(size: 11))
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .padding(.top, 2)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color(.tertiarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 10))
            Spacer(minLength: 40)
        }
    }

    private func assistantRow(_ m: OperatorTeamMessage) -> some View {
        let agent = m.agent_name ?? "bot"
        return VStack(alignment: .leading, spacing: 4) {
            Text(agent)
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .padding(.leading, 4)
            assistantBubble(m, agent: agent)
        }
    }

    @ViewBuilder
    private func assistantBubble(
        _ m: OperatorTeamMessage, agent: String,
    ) -> some View {
        if m.status == "pending" || m.status == "in_progress" {
            HStack(spacing: 6) {
                ProgressView().controlSize(.small)
                Text(m.status == "in_progress" ? "writing…" : "thinking…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(Color(.tertiarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 14))
        } else if m.status == "error" {
            Text(m.error
                 ?? (m.content.isEmpty
                     ? "\(agent) errored." : m.content))
                .font(.system(size: 14))
                .foregroundStyle(.red)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color.red.opacity(0.1))
                .clipShape(RoundedRectangle(cornerRadius: 14))
        } else {
            Text(m.content.isEmpty ? "(empty reply)" : m.content)
                .font(.system(size: 15))
                .foregroundStyle(.primary)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(Color(.tertiarySystemBackground))
                .clipShape(RoundedRectangle(cornerRadius: 14))
        }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 6) {
            if let sendError {
                Text(sendError)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.horizontal, 12)
            }
            HStack(spacing: 8) {
                TextField("Message the team…", text: $draft, axis: .vertical)
                    .textInputAutocapitalization(.sentences)
                    .lineLimit(1...4)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 10)
                    .background(
                        RoundedRectangle(cornerRadius: 18)
                            .strokeBorder(Color(.separator), lineWidth: 1),
                    )
                    .disabled(sending)

                Button {
                    Task { await send() }
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 28))
                        .foregroundStyle(canSend ? Color.accentColor
                                                 : Color(.tertiaryLabel))
                }
                .disabled(!canSend)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
        }
        .background(Color(.systemBackground))
        .overlay(
            Rectangle()
                .fill(Color(.separator))
                .frame(height: 0.5),
            alignment: .top,
        )
    }

    private var canSend: Bool {
        !sending && !draft.trimmingCharacters(
            in: .whitespacesAndNewlines).isEmpty
    }

    // MARK: data

    private func initialLoad() async {
        loading = true
        loadError = nil
        do {
            try await auth.client.switchWorkspace(workspaceID)
            let existing = try await auth.client.listTeamConversations()
            let conv: OperatorTeamConversation
            if let first = existing.first {
                conv = first
            } else {
                conv = try await auth.client.createTeamConversation()
            }
            conversationID = conv.id
            let detail = try await auth.client.fetchTeamConversation(
                id: conv.id,
            )
            messages = detail.messages
            loading = false
            startPollingIfPending()
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
            loading = false
        }
    }

    private func send() async {
        guard canSend, let cid = conversationID else { return }
        let text = draft.trimmingCharacters(
            in: .whitespacesAndNewlines)
        draft = ""
        sending = true
        sendError = nil
        defer { sending = false }
        do {
            let resp = try await auth.client.postTeamMessage(
                conversationID: cid, content: text,
            )
            messages.append(resp.user_message)
            messages.append(resp.router_message)
            messages.append(contentsOf: resp.pending_messages)
            startPollingIfPending()
        } catch {
            sendError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
            draft = text
        }
    }

    private func startPollingIfPending() {
        guard let cid = conversationID else { return }
        guard messages.contains(where: {
            $0.status == "pending" || $0.status == "in_progress"
        }) else { return }
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if Task.isCancelled { break }
                let detail: OperatorTeamConversationDetailResponse
                do {
                    detail = try await auth.client.fetchTeamConversation(
                        id: cid,
                    )
                } catch {
                    continue
                }
                await MainActor.run {
                    messages = detail.messages
                }
                if !detail.messages.contains(where: {
                    $0.status == "pending" || $0.status == "in_progress"
                }) {
                    break
                }
            }
        }
    }
}
