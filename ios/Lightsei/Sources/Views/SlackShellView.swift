// Phase 30.1: Slack/Discord-shaped shell.
//
// Discord-style layout: a narrow server rail pinned on the left
// (one avatar per Constellation) + a main column showing the
// selected server's channel list, which pushes into the existing
// ChatView when a bot channel is tapped.
//
// Built against ChatDataSource so 30.2 can feed it operator data;
// 30.1 wires the end-user source only. The chat pane still takes an
// EndUserVendor (ChatView is vendor-typed until 30.2 generalizes it),
// so the shell resolves the tapped channel's server back to its
// vendor via EndUserChatSource.vendor(for:).

import SwiftUI

struct SlackShellView: View {
    @EnvironmentObject var auth: AuthStore
    let endUser: EndUser

    @State private var source: EndUserChatSource?
    @State private var servers: [ChatServer] = []
    @State private var selectedServerID: String?
    @State private var channels: [ChatChannel] = []
    @State private var loading: Bool = true
    @State private var loadError: String?
    @State private var showAddVendor: Bool = false
    @State private var path: [EndUserVendor] = []

    private let railWidth: CGFloat = 64

    var body: some View {
        HStack(spacing: 0) {
            serverRail
            Divider()
            mainColumn
        }
        .task { await loadServers() }
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
                    Button {
                        showAddVendor = true
                    } label: {
                        ZStack {
                            Circle()
                                .strokeBorder(
                                    Color(.separator),
                                    style: StrokeStyle(lineWidth: 1, dash: [3, 3]),
                                )
                                .frame(width: 44, height: 44)
                            Image(systemName: "plus")
                                .foregroundStyle(Color.accentColor)
                        }
                    }
                    .accessibilityLabel("Add a constellation")
                }
                .padding(.top, 10)
            }

            Spacer(minLength: 0)

            Menu {
                Text(endUser.email)
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
        .background(Color(.secondarySystemBackground))
    }

    private func serverAvatar(_ server: ChatServer) -> some View {
        let selected = server.id == selectedServerID
        return ZStack {
            RoundedRectangle(cornerRadius: selected ? 14 : 22)
                .fill(selected ? Color.accentColor : Color(.tertiarySystemBackground))
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
            Group {
                if loading {
                    ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let loadError {
                    errorState(loadError)
                } else if servers.isEmpty {
                    emptyState
                } else if selectedServerID == nil {
                    pickPrompt
                } else {
                    channelList
                }
            }
            .navigationDestination(for: EndUserVendor.self) { vendor in
                ChatView(vendor: vendor)
            }
        }
        .frame(maxWidth: .infinity)
        .sheet(isPresented: $showAddVendor) {
            AddVendorView {
                Task { await loadServers() }
            }
        }
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
                            Image(systemName: channel.kind == .team ? "number.square" : "number")
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
            Text("Pick a constellation")
                .font(.system(size: 15, weight: .medium))
            Text("Tap a constellation on the left to see its bots.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Text("No constellations yet")
                .font(.system(size: 17, weight: .semibold))
            Text("Add a constellation with an invite code to start chatting with its bots.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Add a constellation") { showAddVendor = true }
                .buttonStyle(.borderedProminent)
                .padding(.top, 4)
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load").font(.system(size: 15, weight: .medium))
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
        let src = source ?? EndUserChatSource(client: auth.client)
        source = src
        do {
            let loaded = try await src.loadServers()
            servers = loaded
            if selectedServerID == nil, let first = loaded.first {
                select(first)
            } else if let sel = selectedServerID {
                // refresh channels for the still-selected server
                if let s = loaded.first(where: { $0.id == sel }) {
                    channels = (try? await src.loadChannels(for: s)) ?? []
                } else {
                    selectedServerID = nil
                    channels = []
                }
            }
            loading = false
        } catch {
            loadError = (error as? LocalizedError)?.errorDescription ?? "\(error)"
            loading = false
        }
    }

    private func select(_ server: ChatServer) {
        selectedServerID = server.id
        Task {
            guard let src = source else { return }
            channels = (try? await src.loadChannels(for: server)) ?? []
        }
    }

    private func openChannel(_ channel: ChatChannel) {
        guard let vendor = source?.vendor(for: channel.serverID) else { return }
        path.append(vendor)
    }
}
