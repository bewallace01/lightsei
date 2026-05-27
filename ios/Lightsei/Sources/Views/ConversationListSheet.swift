// Phase 29.3 polish: conversation list drawer for ChatView.
//
// ChatView auto-loads the most-recent conversation per vendor —
// without this sheet, the user has no way to switch to an older
// thread (the web /c/[slug] page has a left rail for this).
// Sheet shows the conversation list + a "New conversation"
// affordance at the top.

import SwiftUI

struct ConversationListSheet: View {
    @EnvironmentObject var auth: AuthStore
    @Environment(\.dismiss) private var dismiss

    let vendor: EndUserVendor
    /// The conversation currently rendered in ChatView. Used to
    /// highlight the active row in the list.
    let activeID: String?
    var onPick: (String) -> Void
    var onNew: () -> Void

    @State private var state: LoadState = .loading

    enum LoadState {
        case loading
        case ok([EndUserVendorConversation])
        case error(String)
    }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Conversations")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .topBarLeading) {
                        Button("Done") { dismiss() }
                    }
                }
                .task { await load() }
                .refreshable { await load() }
        }
    }

    @ViewBuilder
    private var content: some View {
        switch state {
        case .loading:
            List(0..<4, id: \.self) { _ in
                ConversationRowSkeleton()
            }
            .listStyle(.insetGrouped)
            .allowsHitTesting(false)
        case .error(let msg):
            VStack(spacing: 12) {
                Text(msg)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                Button("Try again") {
                    Task { await load() }
                }
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .ok(let convs):
            List {
                Button {
                    onNew()
                    dismiss()
                } label: {
                    Label("New conversation", systemImage: "square.and.pencil")
                        .foregroundStyle(.tint)
                }
                if convs.isEmpty {
                    Section {
                        Text("No past conversations yet.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity)
                            .listRowBackground(Color.clear)
                    }
                } else {
                    Section("Recent") {
                        ForEach(convs) { c in
                            Button {
                                onPick(c.id)
                                dismiss()
                            } label: {
                                ConversationRow(
                                    conversation: c,
                                    isActive: c.id == activeID,
                                )
                            }
                        }
                    }
                }
            }
            .listStyle(.insetGrouped)
        }
    }

    private func load() async {
        guard let slug = vendor.vendor_slug else {
            state = .error("This vendor isn't fully set up yet.")
            return
        }
        do {
            let resp = try await auth.client.fetchConversations(
                vendorSlug: slug,
            )
            state = .ok(resp.conversations)
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            state = .error(
                (error as? LocalizedError)?.errorDescription
                    ?? "\(error)",
            )
        }
    }
}

private struct ConversationRowSkeleton: View {
    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 5) {
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color(.tertiarySystemFill))
                    .frame(width: 120, height: 13)
                RoundedRectangle(cornerRadius: 4)
                    .fill(Color(.tertiarySystemFill))
                    .frame(width: 60, height: 10)
            }
            Spacer()
        }
        .padding(.vertical, 2)
        .shimmering()
    }
}

private struct ConversationRow: View {
    let conversation: EndUserVendorConversation
    let isActive: Bool

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                Text(
                    conversation.customer_facing_agent_name
                        ?? "Conversation",
                )
                .font(.system(size: 15, weight: .medium))
                .foregroundStyle(.primary)
                Text(relative(conversation.last_message_at))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            Spacer()
            if isActive {
                Image(systemName: "checkmark")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(.tint)
            }
        }
        .padding(.vertical, 2)
    }

    private func relative(_ iso: String) -> String {
        guard let date = ISO8601DateFormatter().date(from: iso) else {
            return iso
        }
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f.localizedString(for: date, relativeTo: Date())
    }
}
