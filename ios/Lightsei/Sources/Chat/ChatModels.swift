// Phase 30.1 + 30.2: identity-agnostic chat-shell models.
//
// The Slack-shaped shell renders three things: a server rail, a
// channel list for the selected server, and a chat pane. These value
// types are what the shell draws; a ChatDataSource (per identity)
// supplies them.
//
// "Server" is the neutral word for a sidebar unit: end users see
// their Constellations, operators see their workspaces.
//
// 30.2 adds ChatTarget so the shell can hand a tapped channel off to
// the right chat pane (end-user widget chat vs. operator threads
// chat) without the shell having to know which identity is signed in.

import Foundation

enum ChannelKind: Hashable {
    case bot   // a direct channel with one bot
    case team  // the whole-team channel (Polaris-routed); 30.3
}

struct ChatServer: Identifiable, Hashable {
    let id: String       // workspace_id
    let name: String
    let unread: Int

    // Single-letter avatar for the rail when there's no logo.
    var initial: String {
        guard let c = name.first else { return "?" }
        return String(c).uppercased()
    }
}

struct ChatChannel: Identifiable, Hashable {
    let id: String           // bot name, or "<serverID>:team" for team
    let name: String
    let kind: ChannelKind
    let serverID: String
}

// The destination a tapped channel pushes onto the shell's nav stack.
// Carries the identity-specific payload the chat pane needs:
//
//   - .endUserVendor    → existing widget ChatView(vendor:)
//   - .operatorBot      → OperatorChatView(workspaceID:agentName:)
//   - .operatorTeam     → OperatorTeamChatView(workspaceID:) — the
//                         Polaris-routed whole-team channel (Phase 30.3.d)
enum ChatTarget: Hashable {
    case endUserVendor(EndUserVendor)
    case operatorBot(workspaceID: String, agentName: String)
    case operatorTeam(workspaceID: String)
}

// Supplies the shell's servers + channels + open-target for whichever
// identity is signed in. Kept @MainActor since implementations hold an
// APIClient + feed SwiftUI state directly.
@MainActor
protocol ChatDataSource {
    func loadServers() async throws -> [ChatServer]
    func loadChannels(for server: ChatServer) async throws -> [ChatChannel]
    func target(for channel: ChatChannel) -> ChatTarget?
}
