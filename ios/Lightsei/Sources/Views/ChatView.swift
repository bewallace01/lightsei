// Phase 29.3: chat thread for one vendor.
//
// Scrollable message list + composer pinned to bottom. Polls the
// widget thread every 3s while open (matches the web /c chat
// cadence; SSE is parked until Phase 28B). On first open, fetches
// the conversation list for the vendor + auto-selects the
// most-recent conversation, or starts fresh if there are none.

import SwiftUI

private let pollInterval: TimeInterval = 3.0

struct ChatView: View {
    @EnvironmentObject var auth: AuthStore
    let vendor: EndUserVendor

    @State private var conversationId: String?
    @State private var messages: [WidgetMessage] = []
    @State private var draft: String = ""
    @State private var sending: Bool = false
    @State private var loadError: String?
    @State private var sendError: String?
    @State private var loaded: Bool = false
    @State private var pollTask: Task<Void, Never>?
    @State private var showSettings: Bool = false

    var body: some View {
        VStack(spacing: 0) {
            messageList
            composer
        }
        .navigationTitle(vendor.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button {
                        startNewConversation()
                    } label: {
                        Label(
                            "New conversation",
                            systemImage: "square.and.pencil",
                        )
                    }
                    Button {
                        showSettings = true
                    } label: {
                        Label(
                            "Vendor settings",
                            systemImage: "gearshape",
                        )
                    }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .accessibilityLabel("Chat menu")
            }
        }
        .task {
            await loadInitial()
            startPolling()
        }
        .onDisappear {
            pollTask?.cancel()
            pollTask = nil
        }
        .sheet(isPresented: $showSettings) {
            VendorSettingsView(vendor: vendor) {
                // Settings changes don't affect thread state, so
                // no reload needed here. The vendor list refreshes
                // itself on next .refreshable.
            }
        }
        .background(Color(.systemBackground).ignoresSafeArea())
    }

    private func startNewConversation() {
        // Bail polling on the old thread + reset state so the
        // composer sends as a fresh conversation. The empty-thread
        // copy renders until the first send + reply pair lands.
        pollTask?.cancel()
        pollTask = nil
        conversationId = nil
        messages = []
        sendError = nil
        startPolling()
    }

    // ---------- messages pane ----------

    private var messageList: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 8) {
                    if !loaded {
                        ProgressView().padding(.top, 24)
                    } else if messages.isEmpty {
                        emptyThread
                    } else {
                        ForEach(messages) { m in
                            ChatBubble(message: m).id(m.id)
                        }
                    }
                    if let loadError {
                        Text(loadError)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            }
            // iOS 16-compatible single-arg form. Deployment target
            // is 16.0 per project.yml; the two-arg form lands in 17+.
            .onChange(of: messages.count) { _ in
                if let last = messages.last?.id {
                    withAnimation {
                        proxy.scrollTo(last, anchor: .bottom)
                    }
                }
            }
        }
    }

    private var emptyThread: some View {
        VStack(spacing: 6) {
            Text("Say hi")
                .font(.system(size: 16, weight: .medium))
            if let agent = vendor.customer_facing_agent_name {
                Text("Chatting with \(agent)")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.top, 80)
    }

    // ---------- composer ----------

    private var composer: some View {
        VStack(spacing: 6) {
            if let sendError {
                Text(sendError)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 16)
            }
            HStack(spacing: 8) {
                TextField("Message", text: $draft, axis: .vertical)
                    .textFieldStyle(.plain)
                    .lineLimit(1...4)
                    .padding(10)
                    .background(
                        RoundedRectangle(cornerRadius: 18)
                            .fill(Color(.secondarySystemBackground)),
                    )
                    .disabled(sending)

                Button {
                    Task { await send() }
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.system(size: 30))
                        .foregroundStyle(
                            canSend ? Color.accentColor : .secondary,
                        )
                }
                .disabled(!canSend || sending)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(.bar)
        }
    }

    private var canSend: Bool {
        !draft.trimmingCharacters(
            in: .whitespacesAndNewlines).isEmpty && !sending
    }

    // ---------- load + poll + send ----------

    private func loadInitial() async {
        guard let slug = vendor.vendor_slug else {
            loadError = "This vendor isn't fully set up yet."
            loaded = true
            return
        }
        do {
            let resp = try await auth.client.fetchConversations(
                vendorSlug: slug,
            )
            // Most-recent conversation lands first per backend
            // ordering (last_message_at DESC); pick that for
            // continuity.
            if let first = resp.conversations.first {
                conversationId = first.id
                if let publicId = vendor.widget_public_id {
                    let thread = try await auth.client.fetchWidgetThread(
                        publicId: publicId,
                        conversationId: first.id,
                    )
                    messages = thread.messages
                }
            }
            loaded = true
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            loadError = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            loaded = true
        }
    }

    private func startPolling() {
        pollTask?.cancel()
        pollTask = Task {
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: UInt64(
                    pollInterval * 1_000_000_000,
                ))
                if Task.isCancelled { break }
                await pollOnce()
            }
        }
    }

    private func pollOnce() async {
        guard
            let publicId = vendor.widget_public_id,
            let convId = conversationId,
            let highestSeen = messages.last?.id
        else { return }
        do {
            let thread = try await auth.client.fetchWidgetThread(
                publicId: publicId,
                conversationId: convId,
                since: highestSeen,
            )
            if !thread.messages.isEmpty {
                messages.append(contentsOf: thread.messages)
            }
        } catch {
            // Poll-tick errors are silent (matches web /c). A
            // persistent failure surfaces on the next send.
        }
    }

    private func send() async {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !sending else { return }
        guard let publicId = vendor.widget_public_id else {
            sendError = "This vendor isn't fully set up yet."
            return
        }
        sending = true
        sendError = nil
        defer { sending = false }
        do {
            let resp = try await auth.client.postWidgetMessage(
                publicId: publicId,
                text: text,
                conversationId: conversationId,
            )
            // First message in a new thread → adopt the backend's
            // new conversation_id so subsequent polls + sends stay
            // on the same thread.
            if conversationId == nil {
                conversationId = resp.conversation_id
            }
            // Optimistically append the user message so the UI
            // updates before the next poll tick.
            messages.append(WidgetMessage(
                id: resp.message_id,
                role: "user",
                text: text,
                sent_at: ISO8601DateFormatter().string(from: Date()),
            ))
            draft = ""
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            sendError = (error as? LocalizedError)?.errorDescription ?? "\(error)"
        }
    }
}

// ---------- bubble ----------

private struct ChatBubble: View {
    let message: WidgetMessage

    var body: some View {
        HStack {
            if alignRight { Spacer(minLength: 40) }
            VStack(alignment: .leading, spacing: 2) {
                if message.role == "system" {
                    Text("system")
                        .font(.system(size: 10).smallCaps())
                        .foregroundStyle(.secondary)
                }
                Text(message.text)
                    .font(.system(size: 15))
                    .padding(.horizontal, 12)
                    .padding(.vertical, 8)
                    .foregroundStyle(textColor)
                    .background(bubbleBackground)
                    .clipShape(RoundedRectangle(cornerRadius: 14))
            }
            if !alignRight { Spacer(minLength: 40) }
        }
    }

    private var alignRight: Bool { message.role == "user" }

    private var textColor: Color {
        switch message.role {
        case "user": return .white
        case "system": return .secondary
        default: return .primary
        }
    }

    private var bubbleBackground: Color {
        switch message.role {
        case "user": return Color.accentColor
        case "operator": return Color.green.opacity(0.15)
        case "system": return Color(.tertiarySystemFill)
        default: return Color(.secondarySystemBackground)  // bot
        }
    }
}
