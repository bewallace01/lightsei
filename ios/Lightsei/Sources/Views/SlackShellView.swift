// Phase 30.1 + 30.2: Slack/Discord-shaped shell, identity-agnostic.
//
// Discord-style layout: a narrow server rail pinned on the left (one
// avatar per Constellation or workspace) + a main column showing the
// selected server's channels, which pushes into an identity-shaped
// chat view when a channel is tapped.
//
// The shell knows nothing about which identity is signed in. A
// ChatDataSource feeds it the servers + channels; tapping a channel
// resolves to a ChatTarget which the shell's navigation destination
// switches on:
//
//   .endUserVendor    → existing ChatView (widget chat)
//   .operatorBot      → OperatorChatView (threads chat)
//
// `addServerAction` is wired only on the end-user surface (Add a
// Constellation via invite code); operators don't yet have an
// analog, so the + button is hidden when nil.

import SwiftUI

// 30.4.e + 30.5.d + 30.6.c + 30.7.b + 30.7.c: per-workspace tab in
// the main column. Operators get Channels + Runs + Agents + Cost +
// Zones today; end-users see only Channels (no operator surfaces
// exist for them). Lives at module scope so callers can pass
// `.channels` as the default.
//
// 30.7.c replaced the segmented Picker with a horizontally-scrolling
// pill strip (see paneModePicker below). The strip scales to N tabs
// without cramping, so adding 30.8 (integrations) + 30.9 (settings)
// is now an enum case + a row in `paneModeOrder` — no shape refactor.
enum MainPaneMode: Hashable {
    case channels
    case runs
    case agents
    case cost
    case zones
    case integrations
    case settings
}

// Display order for the 30.7.c pill strip. File-scoped (not a
// SlackShellView static) because the shell is generic and Swift
// disallows static stored properties on generic types. Add new
// surfaces here (and add their `.case` to MainPaneMode above) to
// extend the strip.
private let paneModeOrder: [(MainPaneMode, String)] = [
    (.channels, "Channels"),
    (.runs, "Runs"),
    (.agents, "Agents"),
    (.cost, "Cost"),
    (.zones, "Zones"),
    (.integrations, "Integrations"),
    (.settings, "Settings"),
]

struct SlackShellView<Source: ChatDataSource & AnyObject>: View {
    @EnvironmentObject var auth: AuthStore
    let source: Source
    let accountLabel: String
    let addServerAction: (() -> Void)?
    let reloadID: Int
    /// When true, the main column shows a Channels|Runs segmented
    /// control above the per-workspace content. The end-user shell
    /// passes false (no Runs surface for end users).
    let showsRunsTab: Bool

    @State private var servers: [ChatServer] = []
    @State private var selectedServerID: String?
    @State private var channels: [ChatChannel] = []
    @State private var mainPaneMode: MainPaneMode = .channels
    @State private var loading: Bool = true
    @State private var loadError: String?
    @State private var path: [ChatTarget] = []

    private let railWidth: CGFloat = 64

    var body: some View {
        ZStack {
            // Phase 31.5.x: every operator surface inherits the
            // cosmos. The rail + main column draw their own
            // translucent backgrounds on top, letting the starfield
            // shine through at the edges.
            StarfieldBackground()
                .ignoresSafeArea()

            HStack(spacing: 0) {
                serverRail
                Divider()
                mainColumn
            }
        }
        .task(id: reloadID) { await loadServers() }
    }

    // MARK: server rail

    private var serverRail: some View {
        VStack(spacing: 12) {
            ScrollView(showsIndicators: false) {
                VStack(spacing: 12) {
                    ForEach(servers) { server in
                        Button {
                            select(server)
                        } label: {
                            serverAvatar(server)
                        }
                        .buttonStyle(.plain)
                    }
                    if let addServerAction {
                        Button {
                            addServerAction()
                        } label: {
                            ZStack {
                                Circle()
                                    .strokeBorder(
                                        Color(.separator),
                                        style: StrokeStyle(
                                            lineWidth: 1, dash: [3, 3],
                                        ),
                                    )
                                    .frame(width: 44, height: 44)
                                Image(systemName: "plus")
                                    .foregroundStyle(Color.accentColor)
                            }
                        }
                        .accessibilityLabel("Add a constellation")
                    }
                }
                .padding(.top, 10)
            }

            Spacer(minLength: 0)

            Menu {
                Text(accountLabel)
                Button("Sign out", role: .destructive) { auth.signOut() }
            } label: {
                Image(systemName: "ellipsis.circle")
                    .font(.system(size: 22))
                    .foregroundStyle(.secondary)
                    .frame(height: 44)
            }
            .accessibilityLabel("Account menu")
        }
        .frame(width: railWidth)
        .frame(maxHeight: .infinity)
        // Translucent black so the starfield reads through at the
        // edges; the rail still feels distinct from the main pane.
        .background(Color.black.opacity(0.35))
    }

    private func serverAvatar(_ server: ChatServer) -> some View {
        let selected = server.id == selectedServerID
        return ZStack {
            RoundedRectangle(cornerRadius: selected ? 14 : 22)
                .fill(selected ? Color.accentColor
                                : Color(.tertiarySystemBackground))
                .frame(width: 44, height: 44)
            Text(server.initial)
                .font(.system(size: 18, weight: .semibold))
                .foregroundStyle(selected ? .white : .primary)
            if server.unread > 0 {
                Text("\(server.unread)")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1)
                    .background(Capsule().fill(Color.red))
                    .offset(x: 16, y: -16)
            }
        }
        .animation(.easeInOut(duration: 0.15), value: selected)
    }

    // MARK: main column

    private var mainColumn: some View {
        NavigationStack(path: $path) {
            VStack(spacing: 0) {
                if showsRunsTab && selectedServerID != nil {
                    paneModePicker
                }
                Group {
                    if loading {
                        ProgressView()
                            .frame(
                                maxWidth: .infinity,
                                maxHeight: .infinity,
                            )
                    } else if let loadError {
                        errorState(loadError)
                    } else if servers.isEmpty {
                        emptyState
                    } else if selectedServerID == nil {
                        pickPrompt
                    } else if mainPaneMode == .runs {
                        // .runs / .agents are only reachable when
                        // showsRunsTab + a selected workspace, so the
                        // force-unwrap is safe.
                        OperatorRunsListView(
                            workspaceID: selectedServerID!,
                        )
                    } else if mainPaneMode == .agents {
                        OperatorAgentsListView(
                            workspaceID: selectedServerID!,
                        )
                    } else if mainPaneMode == .cost {
                        OperatorCostView(
                            workspaceID: selectedServerID!,
                        )
                    } else if mainPaneMode == .zones {
                        OperatorZonesView(
                            workspaceID: selectedServerID!,
                        )
                    } else if mainPaneMode == .integrations {
                        OperatorIntegrationsView(
                            workspaceID: selectedServerID!,
                        )
                    } else if mainPaneMode == .settings {
                        OperatorSettingsView(
                            workspaceID: selectedServerID!,
                        )
                    } else {
                        channelList
                    }
                }
            }
            .navigationDestination(for: ChatTarget.self) { target in
                switch target {
                case .endUserVendor(let vendor):
                    ChatView(vendor: vendor)
                case .operatorBot(let wsID, let agent):
                    OperatorChatView(
                        workspaceID: wsID, agentName: agent,
                    )
                case .operatorTeam(let wsID):
                    OperatorTeamChatView(workspaceID: wsID)
                }
            }
        }
        .frame(maxWidth: .infinity)
    }

    // 30.7.c picker shape refactor. Replaced the segmented Picker
    // (which capped out around 5 labels before cramping) with a
    // horizontally-scrolling pill strip so 30.8 (integrations) +
    // 30.9 (settings) can land without cramping. Selected pill fills
    // with the accent color; others get a subtle tertiary chip. A
    // ScrollViewReader scrolls the active pill into view on change
    // so flipping to an off-screen tab via code (deep link, account
    // switch) still surfaces the selection.
    private var paneModePicker: some View {
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 6) {
                    ForEach(paneModeOrder, id: \.0) { (mode, label) in
                        paneModePill(mode: mode, label: label)
                            .id(mode)
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 8)
                .padding(.bottom, 4)
            }
            .onChange(of: mainPaneMode) { newValue in
                withAnimation(.easeInOut(duration: 0.18)) {
                    proxy.scrollTo(newValue, anchor: .center)
                }
            }
        }
    }

    private func paneModePill(
        mode: MainPaneMode, label: String,
    ) -> some View {
        let selected = mainPaneMode == mode
        return Button {
            mainPaneMode = mode
        } label: {
            Text(label)
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(selected ? .white : .primary)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(
                    selected
                        ? Color.accentColor
                        : Color(.tertiarySystemBackground),
                )
                .clipShape(Capsule())
        }
        .buttonStyle(.plain)
    }

    private var channelList: some View {
        let server = servers.first { $0.id == selectedServerID }
        return List {
            Section(header: Text("Channels")) {
                ForEach(channels) { channel in
                    Button {
                        openChannel(channel)
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName:
                                channel.kind == .team
                                    ? "number.square" : "number",
                            )
                            .foregroundStyle(.secondary)
                            Text(channel.name)
                                .foregroundStyle(.primary)
                            Spacer()
                        }
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle(server?.name ?? "")
        .navigationBarTitleDisplayMode(.inline)
    }

    private var pickPrompt: some View {
        VStack(spacing: 8) {
            Image(systemName: "bubble.left.and.bubble.right")
                .font(.system(size: 34))
                .foregroundStyle(.secondary)
            Text("Pick one to start")
                .font(.system(size: 15, weight: .medium))
            Text("Tap an item on the left to see its bots.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Text("Nothing here yet")
                .font(.system(size: 17, weight: .semibold))
            Text("You haven't joined any spaces yet.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            if let addServerAction {
                Button("Add a constellation") { addServerAction() }
                    .buttonStyle(.borderedProminent)
                    .padding(.top, 4)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load")
                .font(.system(size: 15, weight: .medium))
            Text(msg).font(.caption).foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Retry") { Task { await loadServers() } }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: data

    private func loadServers() async {
        loading = true
        loadError = nil
        do {
            let loaded = try await source.loadServers()
            servers = loaded
            if selectedServerID == nil, let first = loaded.first {
                select(first)
            } else if let sel = selectedServerID {
                if let s = loaded.first(where: { $0.id == sel }) {
                    channels = (try? await source.loadChannels(for: s))
                        ?? []
                } else {
                    selectedServerID = nil
                    channels = []
                }
            }
            loading = false
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
            loading = false
        }
    }

    private func select(_ server: ChatServer) {
        selectedServerID = server.id
        // Reset the per-workspace tab to Channels: switching
        // workspaces while parked on Runs would otherwise show one
        // workspace's Runs above another workspace's avatar, which
        // reads wrong.
        mainPaneMode = .channels
        Task {
            channels = (try? await source.loadChannels(for: server))
                ?? []
        }
    }

    private func openChannel(_ channel: ChatChannel) {
        guard let target = source.target(for: channel) else { return }
        path.append(target)
    }
}
