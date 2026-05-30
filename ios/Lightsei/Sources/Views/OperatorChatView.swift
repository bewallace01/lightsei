// Phase 30.2: operator-side chat with one of the workspace's bots.
//
// Threads-based chat: posting a message creates a user row plus a
// pending assistant row that the deployed agent claims via the
// /agents/{name}/threads/claim worker loop. This view polls the
// thread until the pending row flips to completed (or error).
//
// MVP scope:
//
//   - One persistent thread per (workspace, agent) — the most
//     recently updated thread in the list, created on first visit
//     if none exists.
//   - Poll the thread every 2s while a pending assistant message is
//     outstanding; idle otherwise (poll loop stops cleanly).
//   - Show "thinking..." for the pending row; surface .error rows in
//     red with the backend's message.
//
// If the deployed agent isn't running in the workspace, the assistant
// row stays pending forever — that's a backend operational concern,
// not a client bug, and the view should still render the user's
// message + the spinner without crashing.

import SwiftUI

struct OperatorChatView: View {
    @EnvironmentObject var auth: AuthStore
    let workspaceID: String
    let agentName: String

    @State private var threadID: String?
    @State private var messages: [OperatorThreadMessage] = []
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
        .navigationTitle(agentName)
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
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(messages) { msg in
                            messageRow(msg).id(msg.id)
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

    private func messageRow(_ m: OperatorThreadMessage) -> some View {
        let isUser = m.role == "user"
        return HStack(alignment: .top, spacing: 0) {
            if isUser { Spacer(minLength: 40) }
            VStack(alignment: isUser ? .trailing : .leading, spacing: 2) {
                if m.status == "pending" {
                    HStack(spacing: 6) {
                        ProgressView().controlSize(.small)
                        Text("thinking…")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .background(Color(.tertiarySystemBackground))
                    .clipShape(RoundedRectangle(cornerRadius: 14))
                } else if m.status == "error" {
                    Text(m.error
                         ?? (m.content.isEmpty ? "Bot errored." : m.content))
                        .font(.system(size: 14))
                        .foregroundStyle(.red)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(Color.red.opacity(0.1))
                        .clipShape(RoundedRectangle(cornerRadius: 14))
                } else {
                    Text(m.content)
                        .font(.system(size: 15))
                        .foregroundStyle(isUser ? .white : .primary)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .background(
                            isUser ? Color.accentColor
                                   : Color(.tertiarySystemBackground),
                        )
                        .clipShape(RoundedRectangle(cornerRadius: 14))
                }
            }
            if !isUser { Spacer(minLength: 40) }
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
                TextField("Message \(agentName)…", text: $draft, axis: .vertical)
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
            // Ensure this is the operator's active workspace before
            // hitting /agents/.../threads (which is workspace-scoped).
            try await auth.client.switchWorkspace(workspaceID)
            let existing = try await auth.client.listOperatorThreads(
                agentName: agentName,
            )
            let thread: OperatorThread
            if let first = existing.first {
                thread = first
            } else {
                thread = try await auth.client.createOperatorThread(
                    agentName: agentName,
                )
            }
            threadID = thread.id
            let detail = try await auth.client.fetchOperatorThread(
                id: thread.id,
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
        guard canSend, let tid = threadID else { return }
        let text = draft.trimmingCharacters(
            in: .whitespacesAndNewlines)
        draft = ""
        sending = true
        sendError = nil
        defer { sending = false }
        do {
            let resp = try await auth.client.postOperatorThreadMessage(
                threadID: tid, content: text,
            )
            messages.append(resp.user_message)
            messages.append(resp.pending_message)
            startPollingIfPending()
        } catch {
            sendError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
            // Put the draft back so the user can retry.
            draft = text
        }
    }

    private func startPollingIfPending() {
        guard let tid = threadID else { return }
        guard messages.contains(where: { $0.status == "pending" }) else {
            return
        }
        // Restart the loop so we don't stack overlapping pollers.
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                if Task.isCancelled { break }
                let detail: OperatorThreadDetailResponse
                do {
                    detail = try await auth.client.fetchOperatorThread(
                        id: tid,
                    )
                } catch {
                    continue
                }
                await MainActor.run {
                    messages = detail.messages
                }
                if !detail.messages.contains(where: { $0.status == "pending" }) {
                    break
                }
            }
        }
    }
}
