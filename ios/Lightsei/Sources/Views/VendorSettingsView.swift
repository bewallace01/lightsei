// Phase 29.3 polish: per-vendor settings sheet.
//
// Mirrors /c/[slug]/settings on the web: notification pref +
// unsubscribe. Display-name override is parked here (the web has
// it but it's lower-leverage; revisit when the conversation
// surface starts showing the chosen name).

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

    @State private var notificationPref: String
    @State private var saving: Bool = false
    @State private var error: String?
    @State private var confirmUnlink: Bool = false
    @State private var unlinking: Bool = false

    init(vendor: EndUserVendor, onChanged: @escaping () -> Void) {
        self.vendor = vendor
        self.onChanged = onChanged
        self._notificationPref = State(
            initialValue: vendor.notification_pref ?? "all",
        )
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("Notifications") {
                    Picker("When to notify", selection: $notificationPref) {
                        ForEach(notificationOptions, id: \.value) { opt in
                            Text(opt.label).tag(opt.value)
                        }
                    }
                    .disabled(saving)
                    .onChange(of: notificationPref) { _ in
                        Task { await save() }
                    }
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
                    .disabled(unlinking || saving)
                } footer: {
                    Text("Past conversations stay readable; you won't get new replies until you re-link via a fresh invite code.")
                }
            }
            .navigationTitle(vendor.name)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
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

    private func save() async {
        saving = true
        error = nil
        defer { saving = false }
        do {
            _ = try await auth.client.patchVendorSettings(
                workspaceID: vendor.id,
                notificationPref: notificationPref,
            )
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
