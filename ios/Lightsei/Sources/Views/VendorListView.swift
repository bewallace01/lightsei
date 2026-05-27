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
    @State private var showAddVendor: Bool = false

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
                    ToolbarItem(placement: .topBarLeading) {
                        Button {
                            showAddVendor = true
                        } label: {
                            Image(systemName: "plus")
                        }
                        .accessibilityLabel("Add vendor")
                    }
                    ToolbarItem(placement: .topBarTrailing) {
                        Menu {
                            Button("Sign out", role: .destructive) {
                                auth.signOut()
                            }
                        } label: {
                            Image(systemName: "ellipsis.circle")
                        }
                        .accessibilityLabel("Account menu")
                    }
                }
                .task { await load() }
                .refreshable { await load() }
                .sheet(isPresented: $showAddVendor) {
                    AddVendorView {
                        Task { await load() }
                    }
                }
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
        VStack(spacing: 16) {
            Image(systemName: "tray")
                .font(.system(size: 48))
                .foregroundStyle(.tertiary)
            Text("No vendors yet")
                .font(.system(size: 18, weight: .medium))
            Text(
                "Got an invite code from a vendor? Add it to link their chat.",
            )
            .font(.callout)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 32)
            Button {
                showAddVendor = true
            } label: {
                Text("Enter invite code")
                    .fontWeight(.medium)
                    .padding(.horizontal, 20)
                    .padding(.vertical, 10)
                    .background(Color("AccentColor"))
                    .foregroundStyle(.white)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            }
            .padding(.top, 8)
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
        HStack(alignment: .center, spacing: 14) {
            // Solid indigo tile (matches the app icon shape) with
            // the vendor's initial. Solid + white-on-indigo reads
            // more brand-coherent than the prior tinted circle.
            ZStack {
                RoundedRectangle(cornerRadius: 11)
                    .fill(Color.accentColor)
                Text(initial(for: vendor.name))
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(.white)
            }
            .frame(width: 44, height: 44)

            VStack(alignment: .leading, spacing: 3) {
                Text(vendor.name)
                    .font(.system(size: 16, weight: .semibold))
                    .lineLimit(1)
                if let agent = vendor.customer_facing_agent_name {
                    Text("Chat with \(agent)")
                        .font(.system(size: 13))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer()

            if let count = vendor.unread_count, count > 0 {
                Text("\(count)")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.white)
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3)
                    .background(Color.accentColor, in: Capsule())
            }
        }
    }

    // Use the first non-whitespace character; fall back to "?"
    // for the (extremely unlikely) empty-name case.
    private func initial(for name: String) -> String {
        let trimmed = name.trimmingCharacters(
            in: .whitespacesAndNewlines,
        )
        guard let first = trimmed.first else { return "?" }
        return String(first).uppercased()
    }
}
