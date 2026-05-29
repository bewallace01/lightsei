// Phase 30.1: identity-agnostic chat-shell models.
//
// The Slack-shaped shell renders three things: a server rail, a
// channel list for the selected server, and a chat pane. These
// value types are what the shell draws; a ChatDataSource (per
// identity) supplies them. 30.1 ships only the end-user source
// (EndUserChatSource); 30.2 adds an operator source feeding the
// same shell.
//
// "Server" is the neutral word for a sidebar unit: end users see
// their Constellations, operators will see their workspaces.

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

// Supplies the shell's servers + channels for whichever identity is
// signed in. Kept @MainActor since implementations hold an
// APIClient + feed SwiftUI state directly.
@MainActor
protocol ChatDataSource {
    func loadServers() async throws -> [ChatServer]
    func loadChannels(for server: ChatServer) async throws -> [ChatChannel]
}
