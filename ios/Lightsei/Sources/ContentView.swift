// Phase 30.2: root view, switches on dual-identity AuthStore state.
//
// Four cases:
//
//   .unknown          → splash while restore() runs (avoids a flash
//                       of SignInView for returning users).
//   .signedOut        → SignInView (Customer + Business lanes).
//   .endUser(user)    → SlackShellView fed by EndUserChatSource.
//   .operatorUser(op) → SlackShellView fed by OperatorChatSource.

import SwiftUI

struct ContentView: View {
    @EnvironmentObject var auth: AuthStore

    var body: some View {
        switch auth.state {
        case .unknown:
            SplashView()
        case .signedOut:
            SignInView()
        case .endUser(let user):
            EndUserShell(endUser: user)
        case .operatorUser(let identity):
            OperatorShell(identity: identity)
        }
    }
}

// End-user wrapper: owns the EndUserChatSource + Add-Constellation
// sheet, increments reloadID after a successful redeem so the shell
// re-fetches the server list.
private struct EndUserShell: View {
    @EnvironmentObject var auth: AuthStore
    let endUser: EndUser

    @State private var source: EndUserChatSource?
    @State private var showAddVendor: Bool = false
    @State private var reloadID: Int = 0

    var body: some View {
        Group {
            if let source {
                SlackShellView(
                    source: source,
                    accountLabel: endUser.email,
                    addServerAction: { showAddVendor = true },
                    reloadID: reloadID,
                )
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear {
            if source == nil {
                source = EndUserChatSource(client: auth.client)
            }
        }
        .sheet(isPresented: $showAddVendor) {
            AddVendorView { reloadID &+= 1 }
        }
    }
}

// Operator wrapper: owns the OperatorChatSource. No add-server flow
// yet on the operator side (workspaces are created via the dashboard
// / web signup); the rail's + button is hidden when addServerAction
// is nil.
private struct OperatorShell: View {
    @EnvironmentObject var auth: AuthStore
    let identity: AuthStore.OperatorIdentity

    @State private var source: OperatorChatSource?

    var body: some View {
        Group {
            if let source {
                SlackShellView(
                    source: source,
                    accountLabel: identity.user.email,
                    addServerAction: nil,
                    reloadID: 0,
                )
            } else {
                ProgressView()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .onAppear {
            if source == nil {
                source = OperatorChatSource(client: auth.client)
            }
        }
    }
}

private struct SplashView: View {
    var body: some View {
        ZStack {
            Color(.systemBackground).ignoresSafeArea()
            VStack(spacing: 14) {
                ZStack {
                    RoundedRectangle(cornerRadius: 16)
                        .fill(Color.accentColor)
                    Text("L")
                        .font(.system(
                            size: 36, weight: .semibold,
                        ))
                        .foregroundStyle(.white)
                }
                .frame(width: 72, height: 72)

                Text("Lightsei")
                    .font(.system(
                        size: 22, weight: .semibold, design: .serif,
                    ))
                ProgressView()
                    .controlSize(.small)
                    .padding(.top, 4)
            }
        }
    }
}

#Preview {
    ContentView().environmentObject(AuthStore())
}
