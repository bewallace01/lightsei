// Phase 29.1: placeholder root view.
//
// Just enough surface to confirm the project builds, links against
// SwiftUI, and renders in the simulator. Phase 29.3 swaps this for
// the real vendor-list + chat surface that mirrors the /c web app.

import SwiftUI

struct ContentView: View {
    var body: some View {
        ZStack {
            // Match the consumer surface's white background so the
            // visual identity tracks the web /c experience.
            Color(.systemBackground).ignoresSafeArea()

            VStack(spacing: 24) {
                // Star glyph stand-in for the constellation brand mark.
                // Replaced by a real asset (matching the operator
                // dashboard's celestial trio) once the icon set is
                // generated.
                Image(systemName: "sparkle")
                    .font(.system(size: 64, weight: .light))
                    .foregroundStyle(.tint)

                Text("Lightsei")
                    .font(.system(size: 34, weight: .semibold, design: .serif))

                Text("Phase 29.1 scaffold")
                    .font(.system(size: 14))
                    .foregroundStyle(.secondary)
            }
        }
    }
}

#Preview {
    ContentView()
}
