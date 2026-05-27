// Phase 29.3 polish: per-vendor settings sheet.
//
// Mirrors /c/[slug]/settings on the web: notification pref +
// display-name override + unsubscribe. Hydrates from the
// /me/end-user/vendors/{slug} endpoint on mount so the real
// link settings render (the list endpoint omits them).

import SwiftUI

private let notificationOptions: [(value: String, label: String)] = [
    ("all", "All replies"),
    ("mentions", "Only @-mentions (coming soon)"),
    ("off", "Off"),
]

struct VendorSettingsView: View {
    @EnvironmentObject var auth: AuthStore
    @Environment(\.dismiss) private var dismiss

    let vendor: EndUserVendor
    /// Called after a successful save or unlink so the caller can
    /// refresh + pop back.
    var onChanged: () -> Void

    // Live state, hydrated by load() on mount. Picker / TextField
    // bind to these; auto-save fires on commit.
    @State private var notificationPref: String = "all"
    @State private var displayName: String = ""
    @State private var savedDisplayName: String = ""

    @State private var loaded: Bool = false
    @State private var savingPref: Bool = false
    @State private var savingName: Bool = false
    @State private var error: String?
    @State private var confirmUnlink: Bool = false
    @State private var unlinking: Bool = false

    var body: some View {
        NavigationStack {
            Form {
                Section("Notifications") {
                    Picker(
                        "When to notify", selection: $notificationPref,
                    ) {
                        ForEach(notificationOptions, id: \.value) { opt in
                            Text(opt.label).tag(opt.value)
                        }
                    }
                    .disabled(!loaded || savingPref)
                    .onChange(of: notificationPref) { newValue in
                        guard loaded else { return }
                        Task { await savePref(newValue) }
                    }
                }

                Section {
                    HStack {
                        TextField(
                            "How the bot should address you",
                            text: $displayName,
                        )
                        .textInputAutocapitalization(.words)
                        .disabled(!loaded || savingName)
                        if savingName {
                            ProgressView().controlSize(.small)
                        }
                    }
                    .onSubmit {
                        Task { await saveName() }
                    }
                    Button("Save name") {
                        Task { await saveName() }
                    }
                    .disabled(
                        !loaded || savingName
                            || displayName == savedDisplayName,
                    )
                } header: {
                    Text("Display name")
                } footer: {
                    Text(
                        "What \(vendor.customer_facing_agent_name ?? "the bot") will call you.",
                    )
                }

                if let error {
                    Section {
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                }

                Section {
                    Button(role: .destructive) {
                        confirmUnlink = true
                    } label: {
                        if unlinking {
                            ProgressView()
                        } else {
                            Text("Unsubscribe from \(vendor.name)")
                        }
                    }
                    .disabled(unlinking || savingPref || savingName)
                } footer: {
                    Text(
                        "Past conversations stay readable; you won't get new replies until you re-link via a fresh invite code.",
                    )
                }
            }
            .navigationTitle(vendor.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .task { await load() }
            .confirmationDialog(
                "Unsubscribe from \(vendor.name)?",
                isPresented: $confirmUnlink,
                titleVisibility: .visible,
            ) {
                Button("Unsubscribe", role: .destructive) {
                    Task { await unlink() }
                }
                Button("Cancel", role: .cancel) {}
            }
        }
    }

    private func load() async {
        guard let slug = vendor.vendor_slug else {
            loaded = true
            return
        }
        do {
            let full = try await auth.client.fetchVendor(slug: slug)
            notificationPref = full.notification_pref ?? "all"
            displayName = full.display_name_override ?? ""
            savedDisplayName = displayName
            loaded = true
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            self.error = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
            loaded = true
        }
    }

    private func savePref(_ pref: String) async {
        savingPref = true
        error = nil
        defer { savingPref = false }
        do {
            _ = try await auth.client.patchVendorSettings(
                workspaceID: vendor.id,
                notificationPref: pref,
            )
            onChanged()
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            self.error = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }

    private func saveName() async {
        let trimmed = displayName.trimmingCharacters(
            in: .whitespacesAndNewlines,
        )
        guard trimmed != savedDisplayName else { return }
        savingName = true
        error = nil
        defer { savingName = false }
        do {
            // Empty string clears the override per backend's
            // PATCH /me/end-user/vendors/{id} contract; nil leaves
            // it unchanged. Send empty when the user clears the
            // field.
            _ = try await auth.client.patchVendorSettings(
                workspaceID: vendor.id,
                displayName: trimmed,
            )
            displayName = trimmed
            savedDisplayName = trimmed
            onChanged()
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            self.error = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }

    private func unlink() async {
        unlinking = true
        error = nil
        defer { unlinking = false }
        do {
            _ = try await auth.client.unlinkVendor(
                workspaceID: vendor.id,
            )
            onChanged()
            dismiss()
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            self.error = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }
}
