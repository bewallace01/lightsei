// Phase 29.3: post-signin home screen.
//
// Lists the linked vendors. Tap a vendor card to push into ChatView.
// Sign-out lives in the toolbar so a returning user can land here
// and reset auth without hunting through a settings menu.
//
// Mirrors the web /c page's vendor card design.

import SwiftUI

struct VendorListView: View {
    @EnvironmentObject var auth: AuthStore
    let endUser: EndUser

    @State private var state: LoadState = .loading

    enum LoadState {
        case loading
        case ok([EndUserVendor])
        case error(String)
    }

    var body: some View {
        NavigationStack {
            content
                .navigationTitle("Your chats")
                .toolbar {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button("Sign out", role: .destructive) {
                            auth.signOut()
                        }
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
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        case .error(let msg):
            VStack(spacing: 12) {
                Text(msg)
                    .font(.callout)
                    .foregroundStyle(.red)
                    .multilineTextAlignment(.center)
                Button("Try again") { Task { await load() } }
            }
            .padding()
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .ok(let vendors):
            if vendors.isEmpty {
                emptyState
            } else {
                vendorList(vendors)
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 12) {
            Text("No vendors yet")
                .font(.system(size: 18, weight: .medium))
            Text(
                "When a vendor invites you, their chat will appear here.",
            )
            .font(.callout)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private func vendorList(_ vendors: [EndUserVendor]) -> some View {
        List(vendors) { vendor in
            NavigationLink(value: vendor) {
                VendorRow(vendor: vendor)
            }
            .listRowInsets(EdgeInsets(
                top: 12, leading: 16, bottom: 12, trailing: 16,
            ))
        }
        .listStyle(.plain)
        .navigationDestination(for: EndUserVendor.self) { vendor in
            ChatView(vendor: vendor)
        }
    }

    private func load() async {
        do {
            let vendors = try await auth.client.fetchVendors()
            state = .ok(vendors)
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            state = .error(
                (error as? LocalizedError)?.errorDescription ?? "\(error)",
            )
        }
    }
}

private struct VendorRow: View {
    let vendor: EndUserVendor

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            ZStack {
                Circle().fill(Color.accentColor.opacity(0.15))
                Text(vendor.name.prefix(1).uppercased())
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(.tint)
            }
            .frame(width: 44, height: 44)

            VStack(alignment: .leading, spacing: 2) {
                Text(vendor.name)
                    .font(.system(size: 16, weight: .medium))
                if let agent = vendor.customer_facing_agent_name {
                    Text("Chat with \(agent)")
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                }
            }

            Spacer()

            if let count = vendor.unread_count, count > 0 {
                Text("\(count)")
                    .font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Color.accentColor, in: Capsule())
                    .foregroundStyle(.white)
            }
        }
    }
}
