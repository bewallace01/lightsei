// Phase 29.3 polish: invite-code redemption sheet.
//
// Without this, a freshly-signed-in user lands on an empty vendor
// list with no in-app way to link a vendor — they'd have to bounce
// to the web /c page, redeem there, then come back. Matches the
// AddVendorModal on the web /c page.

import SwiftUI

struct AddVendorView: View {
    @EnvironmentObject var auth: AuthStore
    @Environment(\.dismiss) private var dismiss

    /// Called on successful redeem so the caller can refresh the
    /// vendor list before the sheet auto-dismisses.
    var onRedeemed: () -> Void

    @State private var code: String = ""
    @State private var submitting: Bool = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            VStack(alignment: .leading, spacing: 16) {
                Text("Paste the invite code the vendor sent you.")
                    .font(.callout)
                    .foregroundStyle(.secondary)

                TextField("inv-...", text: $code)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .font(.system(size: 16, design: .monospaced))
                    .padding(12)
                    .background(
                        RoundedRectangle(cornerRadius: 8)
                            .strokeBorder(
                                Color(.separator), lineWidth: 1,
                            ),
                    )
                    .disabled(submitting)

                if let error {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                }

                Spacer()
            }
            .padding(20)
            .navigationTitle("Add a vendor")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Cancel") { dismiss() }
                        .disabled(submitting)
                }
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        Task { await submit() }
                    } label: {
                        if submitting {
                            ProgressView()
                        } else {
                            Text("Add").fontWeight(.semibold)
                        }
                    }
                    .disabled(
                        submitting
                        || code.trimmingCharacters(
                            in: .whitespacesAndNewlines,
                        ).isEmpty,
                    )
                }
            }
        }
    }

    private func submit() async {
        let trimmed = code.trimmingCharacters(
            in: .whitespacesAndNewlines,
        )
        guard !trimmed.isEmpty else { return }
        submitting = true
        error = nil
        defer { submitting = false }
        do {
            _ = try await auth.client.redeemInvite(code: trimmed)
            onRedeemed()
            dismiss()
        } catch APIError.unauthorized {
            auth.signOut()
        } catch {
            self.error = (error as? LocalizedError)?
                .errorDescription ?? "\(error)"
        }
    }
}
