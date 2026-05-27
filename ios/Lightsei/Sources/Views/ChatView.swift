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
    @State private var showConversations: Bool = false
    // Phase 29.3 polish: surface a discreet "reconnecting"
    // chip when the 3s poll fails repeatedly so the user doesn't
    // wonder why no new replies are landing.
    @State private var consecutivePollFailures: Int = 0
    // Phase 29.3 polish: track whether the bottom of the thread
    // is currently in the viewport so that auto-scroll only fires
    // when the user is following the conversation. When the user
    // has scrolled up + new messages arrive, surface a floating
    // "jump to bottom" button instead of yanking them down.
    @State private var bottomVisible: Bool = true
    @State private var unseenIncoming: Int = 0

    var body: some View {
        VStack(spacing: 0) {
            if consecutivePollFailures >= 3 {
                reconnectingChip
            }
            messageList
                .refreshable {
                    // Pull-to-refresh re-fetches the full conversation
                    // list (in case a new thread was started from
                    // the web) + the active thread from scratch
                    // (covers the case where the 3s poll missed
                    // an in-between message).
                    await loadInitial()
                }
            composer
        }
        .navigationTitle(vendor.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            // Leading: conversation list drawer. Tap a row to
            // switch threads; long-press the chat menu later for
            // bulk actions (parking lot).
            ToolbarItem(placement: .topBarLeading) {
                Button {
                    showConversations = true
                } label: {
                    Image(systemName: "list.bullet.rectangle")
                }
                .accessibilityLabel("Conversations")
            }
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
        .sheet(isPresented: $showConversations) {
            ConversationListSheet(
                vendor: vendor,
                activeID: conversationId,
                onPick: { id in switchTo(conversationID: id) },
                onNew: { startNewConversation() },
            )
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

    private func switchTo(conversationID id: String) {
        guard id != conversationId else { return }
        pollTask?.cancel()
        pollTask = nil
        conversationId = id
        messages = []
        sendError = nil
        loaded = false
        Task {
            await loadConversation(id: id)
            startPolling()
        }
    }

    /// Load a specific conversation by id (vs. the auto-pick on
    /// initial mount). Used by the conversation drawer.
    private func loadConversation(id: String) async {
        guard let publicId = vendor.widget_public_id else {
            loadError = "This vendor isn't fully set up yet."
            loaded = true
            return
        }
        do {
            let thread = try await auth.client.fetchWidgetThread(
                publicId: publicId,
                conversationId: id,
            )
            messages = thread.messages
            loaded = true
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
            loaded = true
        }
    }

    // ---------- messages pane ----------

    private var messageList: some View {
        ScrollViewReader { proxy in
            ZStack(alignment: .bottom) {
                ScrollView {
                    LazyVStack(spacing: 4) {
                        if !loaded {
                            ThreadSkeleton()
                        } else if messages.isEmpty {
                            emptyThread
                        } else {
                            ForEach(decorated(messages)) { item in
                                if let stamp = item.timestampHeader {
                                    Text(stamp)
                                        .font(.caption2)
                                        .foregroundStyle(.tertiary)
                                        .frame(maxWidth: .infinity)
                                        .padding(.top, 12)
                                        .padding(.bottom, 4)
                                        .id("ts-\(item.message.id)")
                                }
                                ChatBubble(
                                    message: item.message,
                                    isPending: item.message.id == pendingMessageID,
                                )
                                .id(item.message.id)
                            }
                            // Invisible sentinel after the last
                            // bubble. iOS fires .onAppear /
                            // .onDisappear as it scrolls into +
                            // out of the viewport, which we use
                            // as a proxy for "the user is at the
                            // bottom following the thread."
                            Color.clear
                                .frame(height: 1)
                                .id("__bottom__")
                                .onAppear {
                                    bottomVisible = true
                                    unseenIncoming = 0
                                }
                                .onDisappear {
                                    bottomVisible = false
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
                // iOS 16-compatible single-arg form. Deployment
                // target is 16.0; the two-arg form lands in 17+.
                .onChange(of: messages.count) { _ in
                    guard let last = messages.last?.id else { return }
                    if bottomVisible {
                        // User is following along — slide them to
                        // the new message.
                        withAnimation {
                            proxy.scrollTo(last, anchor: .bottom)
                        }
                    } else if let role = messages.last?.role,
                              role != "user" {
                        // User is reading older messages + a new
                        // bot/operator reply landed. Don't yank
                        // their place; surface a chip instead.
                        unseenIncoming += 1
                    }
                }

                if !bottomVisible && unseenIncoming > 0 {
                    jumpToBottomChip {
                        withAnimation {
                            proxy.scrollTo(
                                "__bottom__", anchor: .bottom,
                            )
                        }
                        unseenIncoming = 0
                    }
                    .padding(.bottom, 12)
                    .transition(
                        .move(edge: .bottom).combined(with: .opacity),
                    )
                }
            }
        }
    }

    private func jumpToBottomChip(_ action: @escaping () -> Void)
        -> some View
    {
        Button(action: action) {
            HStack(spacing: 6) {
                Image(systemName: "arrow.down")
                    .font(.system(size: 12, weight: .semibold))
                Text(
                    unseenIncoming == 1
                        ? "1 new message"
                        : "\(unseenIncoming) new messages",
                )
                .font(.system(size: 13, weight: .medium))
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .foregroundStyle(.white)
            .background(Color.accentColor, in: Capsule())
            .shadow(color: .black.opacity(0.18), radius: 6, y: 2)
        }
        .accessibilityLabel(
            "Jump to bottom (\(unseenIncoming) new)",
        )
    }

    // Group consecutive messages + decide which ones need a
    // timestamp header above them. Pattern matches iOS Messages:
    // first message of the day gets a "Mon · 3:14 PM" stamp; later
    // messages within 5 minutes inherit the stamp from above (no
    // repetition). Reduces visual clutter on a chatty thread.
    private func decorated(
        _ msgs: [WidgetMessage],
    ) -> [ChatItem] {
        let gap: TimeInterval = 5 * 60
        let fmt = ISO8601DateFormatter()
        var out: [ChatItem] = []
        var lastTs: Date?
        for m in msgs {
            let ts = fmt.date(from: m.sent_at)
            let needsHeader: Bool = {
                guard let ts else { return false }
                guard let last = lastTs else { return true }
                return ts.timeIntervalSince(last) > gap
            }()
            out.append(ChatItem(
                message: m,
                timestampHeader: needsHeader
                    ? Self.formatStamp(ts ?? Date())
                    : nil,
            ))
            if let ts { lastTs = ts }
        }
        return out
    }

    private static func formatStamp(_ d: Date) -> String {
        let cal = Calendar.current
        let f = DateFormatter()
        if cal.isDateInToday(d) {
            f.dateFormat = "h:mm a"
        } else if cal.isDateInYesterday(d) {
            f.dateFormat = "'Yesterday' h:mm a"
        } else {
            f.dateFormat = "EEE MMM d · h:mm a"
        }
        return f.string(from: d)
    }

    // The optimistic message id from send(). When non-nil, the
    // bubble at that id renders faded until a real reply lands.
    private var pendingMessageID: Int? {
        if sending, let last = messages.last, last.role == "user" {
            return last.id
        }
        return nil
    }

    private var reconnectingChip: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.mini)
            Text("Reconnecting…")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity)
        .background(Color(.secondarySystemBackground))
    }

    private var emptyThread: some View {
        VStack(spacing: 10) {
            ZStack {
                Circle()
                    .fill(Color.accentColor.opacity(0.12))
                Image(systemName: "bubble.left.and.bubble.right")
                    .font(.system(size: 28))
                    .foregroundStyle(.tint)
            }
            .frame(width: 72, height: 72)

            Text("Say hi")
                .font(.system(size: 18, weight: .semibold))

            if let agent = vendor.customer_facing_agent_name {
                Text("\(agent) is ready to help.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
        }
        .padding(.top, 80)
        .padding(.bottom, 20)
    }

    // ---------- composer ----------

    private var composer: some View {
        VStack(spacing: 6) {
            if let sendError {
                HStack(spacing: 8) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .foregroundStyle(.red)
                    Text(sendError)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                    Spacer()
                    if !draft.isEmpty {
                        Button("Retry") {
                            Task { await send() }
                        }
                        .font(.caption.weight(.medium))
                    }
                }
                .padding(.horizontal, 16)
                .padding(.vertical, 4)
                .background(Color.red.opacity(0.05))
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
                // New message arrival → light haptic + clear any
                // reconnecting chip from prior failed ticks.
                UIImpactFeedbackGenerator(style: .light)
                    .impactOccurred()
            }
            consecutivePollFailures = 0
        } catch {
            // Poll-tick errors silent in logs (matches web /c) but
            // bump the counter so the UI surfaces a "reconnecting"
            // chip after a few consecutive failures.
            consecutivePollFailures += 1
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
        // Tactile feedback the moment the tap registers; the
        // request is async + the optimistic message appears below
        // but a light tap-confirmation reads faster than waiting
        // for the bubble to draw.
        UIImpactFeedbackGenerator(style: .light).impactOccurred()
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

// Skeleton thread: 4 alternating bubble shapes with shimmer.
// Bubble widths are deterministic per-index so the placeholder
// reads as "a real thread, just unloaded" rather than a single
// repeating block.
private struct ThreadSkeleton: View {
    var body: some View {
        VStack(spacing: 10) {
            ForEach(0..<4) { idx in
                row(at: idx)
            }
        }
        .padding(.top, 12)
        .shimmering()
    }

    @ViewBuilder
    private func row(at idx: Int) -> some View {
        let rightAligned = idx == 1 || idx == 3
        let widths: [CGFloat] = [220, 180, 260, 140]
        HStack {
            if rightAligned { Spacer(minLength: 40) }
            RoundedRectangle(cornerRadius: 14)
                .fill(Color(.tertiarySystemFill))
                .frame(width: widths[idx], height: 36)
            if !rightAligned { Spacer(minLength: 40) }
        }
    }
}

// ---------- bubble ----------

// Decorated row used by the scroll list. A `timestampHeader` carries
// a "Mon · 3:14 PM" string that renders above the bubble when the
// row starts a new time chunk.
private struct ChatItem: Identifiable {
    let message: WidgetMessage
    let timestampHeader: String?
    var id: Int { message.id }
}

private struct ChatBubble: View {
    let message: WidgetMessage
    /// When true (set by ChatView for an optimistic user-side row
    /// that hasn't yet round-tripped to the backend), the bubble
    /// renders faded so the user knows the send is in flight.
    let isPending: Bool

    init(message: WidgetMessage, isPending: Bool = false) {
        self.message = message
        self.isPending = isPending
    }

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
                    .opacity(isPending ? 0.55 : 1)
                    // iOS native long-press bubble menu. iOS surfaces
                    // Copy automatically + we add Share for sending
                    // a bot reply elsewhere. Matches the affordance
                    // every iOS user expects on a chat bubble.
                    .contextMenu {
                        Button {
                            UIPasteboard.general.string = message.text
                        } label: {
                            Label("Copy", systemImage: "doc.on.doc")
                        }
                        ShareLink(item: message.text) {
                            Label(
                                "Share", systemImage:
                                    "square.and.arrow.up",
                            )
                        }
                    }
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
