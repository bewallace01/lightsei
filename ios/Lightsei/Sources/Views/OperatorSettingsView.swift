// Phase 30.9.b: operator-side Settings view.
//
// Single screen, four sections. Read-only this iteration — edits
// (workspace rename, vendor slug claim, billing portal) all live
// on web. The view nudges the operator there for anything requiring
// a form.
//
//   Workspace        name, created date, Constellation slug (if
//                    claimed) or "(not claimed yet)" placeholder
//   Plan             plan tier pill, free credits remaining,
//                    monthly budget cap if set, Stripe customer
//                    state. "Manage billing on web" footer link.
//   Account          signed-in email + Sign Out. Sign Out
//                    duplicates the rail's account menu — Settings
//                    is the iOS-conventional home for it.
//   Configure on web one-line footer with the dashboard URL.

import SwiftUI

struct OperatorSettingsView: View {
    @EnvironmentObject var auth: AuthStore
    let workspaceID: String

    @State private var ws: OperatorWorkspaceMe?
    @State private var loading: Bool = true
    @State private var loadError: String?
    @State private var signingOut: Bool = false

    var body: some View {
        Group {
            if loading && ws == nil {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadError, ws == nil {
                errorState(loadError)
            } else if let ws {
                content(ws)
            }
        }
        .task { await load() }
        .refreshable { await load() }
    }

    private func content(_ w: OperatorWorkspaceMe) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                workspaceSection(w)
                planSection(w)
                accountSection
                configureOnWebFooter
            }
            .padding(16)
        }
    }

    // MARK: workspace

    private func workspaceSection(
        _ w: OperatorWorkspaceMe,
    ) -> some View {
        section("Workspace") {
            VStack(spacing: 0) {
                kvRow("Name", w.name)
                Divider().padding(.leading, 12)
                kvRow("Created", absoluteShortDate(w.created_at))
                Divider().padding(.leading, 12)
                kvRow(
                    "Constellation slug",
                    w.vendor_slug ?? "(not claimed yet)",
                    valueDimmed: w.vendor_slug == nil,
                )
            }
            .background(Color(.secondarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
    }

    // MARK: plan

    private func planSection(_ w: OperatorWorkspaceMe) -> some View {
        section("Plan") {
            VStack(spacing: 0) {
                HStack(alignment: .center, spacing: 8) {
                    Text("Tier")
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: 130, alignment: .leading)
                    planTierPill(w.plan_tier)
                    Spacer()
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                Divider().padding(.leading, 12)
                kvRow(
                    "Free credits",
                    currency(w.free_credits_remaining_usd),
                )
                if let cap = w.budget_usd_monthly {
                    Divider().padding(.leading, 12)
                    kvRow("Monthly cap", currency(cap))
                }
                Divider().padding(.leading, 12)
                kvRow(
                    "Billing",
                    w.has_stripe_customer
                        ? "Customer in Stripe"
                        : "Not yet a Stripe customer",
                    valueDimmed: !w.has_stripe_customer,
                )
            }
            .background(Color(.secondarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 10))

            Text("Subscribe or manage billing on the web dashboard. Stripe's portal works best in a desktop browser.")
                .font(.caption2)
                .foregroundStyle(.secondary)
                .padding(.top, 6)
                .padding(.horizontal, 4)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private func planTierPill(_ tier: String) -> some View {
        let display: String
        let bg: Color
        let fg: Color
        switch tier.lowercased() {
        case "pro", "team", "enterprise":
            display = tier.capitalized
            bg = Color.accentColor.opacity(0.15)
            fg = Color.accentColor
        default:
            // "free" + anything we don't recognize.
            display = tier.isEmpty ? "Free" : tier.capitalized
            bg = Color.gray.opacity(0.15)
            fg = .secondary
        }
        return Text(display)
            .font(.system(size: 11, weight: .semibold))
            .foregroundStyle(fg)
            .padding(.horizontal, 8)
            .padding(.vertical, 2)
            .background(bg)
            .clipShape(Capsule())
    }

    // MARK: account

    private var accountSection: some View {
        section("Account") {
            VStack(spacing: 0) {
                kvRow("Signed in as", accountEmail() ?? "—")
                Divider().padding(.leading, 12)
                Button(role: .destructive) {
                    Task { await signOut() }
                } label: {
                    HStack {
                        Text(signingOut ? "Signing out…" : "Sign out")
                            .font(.system(size: 14, weight: .medium))
                            .foregroundStyle(.red)
                        Spacer()
                        if signingOut {
                            ProgressView().controlSize(.small)
                        }
                    }
                    .padding(.horizontal, 12)
                    .padding(.vertical, 12)
                    .contentShape(Rectangle())
                }
                .disabled(signingOut)
            }
            .background(Color(.secondarySystemBackground))
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
    }

    private func accountEmail() -> String? {
        if case .operatorUser(let identity) = auth.state {
            return identity.user.email
        }
        return nil
    }

    // MARK: footer

    private var configureOnWebFooter: some View {
        HStack(spacing: 6) {
            Image(systemName: "info.circle")
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            Text("Edit anything else on app.lightsei.com.")
                .font(.caption2)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.top, 4)
    }

    // MARK: scaffolding

    private func section<C: View>(
        _ title: String,
        @ViewBuilder _ body: () -> C,
    ) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title.uppercased())
                .font(.system(size: 11, weight: .semibold))
                .foregroundStyle(.secondary)
                .tracking(0.5)
            body()
        }
    }

    private func kvRow(
        _ label: String, _ value: String,
        valueDimmed: Bool = false,
    ) -> some View {
        HStack(alignment: .center, spacing: 8) {
            Text(label)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(.secondary)
                .frame(width: 130, alignment: .leading)
            Text(value)
                .font(.system(size: 13))
                .foregroundStyle(valueDimmed ? .secondary : .primary)
                .italic(valueDimmed)
            Spacer()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
    }

    private func errorState(_ msg: String) -> some View {
        VStack(spacing: 10) {
            Text("Couldn't load settings")
                .font(.system(size: 15, weight: .medium))
            Text(msg)
                .font(.caption)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Retry") { Task { await load() } }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: data + actions

    private func load() async {
        loading = true
        loadError = nil
        do {
            try await auth.client.switchWorkspace(workspaceID)
            ws = try await auth.client.fetchWorkspaceMe()
        } catch {
            loadError = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
        loading = false
    }

    private func signOut() async {
        signingOut = true
        // AuthStore.signOut() flips state synchronously; ContentView
        // re-renders to SignInView. We don't need to do anything
        // after — the whole view tree gets replaced.
        auth.signOut()
        signingOut = false
    }
}

// MARK: helpers

private func absoluteShortDate(_ d: Date) -> String {
    let f = DateFormatter()
    f.dateStyle = .medium
    f.timeStyle = .none
    return f.string(from: d)
}

private func currency(_ v: Double) -> String {
    if v > 0 && v < 0.01 {
        return String(format: "$%.4f", v)
    }
    return String(format: "$%.2f", v)
}
