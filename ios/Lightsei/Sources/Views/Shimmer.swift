// Phase 29.3 polish: shimmer modifier for skeleton loading states.
//
// SwiftUI's built-in .redacted(reason: .placeholder) gives the
// boxed-out-text look but it's static — a moving sheen reads as
// "loading" much faster, especially in the half-second between
// view mount and the first network response.
//
// Usage:
//   YourSkeletonView()
//       .redacted(reason: .placeholder)
//       .shimmering()

import SwiftUI

extension View {
    /// Overlay a slow horizontal sheen via a mask + linear
    /// gradient. Pair with .redacted(reason: .placeholder) for
    /// the full skeleton effect.
    func shimmering(
        active: Bool = true,
        duration: Double = 1.4,
    ) -> some View {
        modifier(Shimmer(active: active, duration: duration))
    }
}

private struct Shimmer: ViewModifier {
    let active: Bool
    let duration: Double

    @State private var phase: CGFloat = -1

    func body(content: Content) -> some View {
        if !active {
            content
        } else {
            content
                .overlay(
                    GeometryReader { geo in
                        LinearGradient(
                            colors: [
                                .clear,
                                Color.white.opacity(0.55),
                                .clear,
                            ],
                            startPoint: .leading,
                            endPoint: .trailing,
                        )
                        .frame(width: geo.size.width * 1.5)
                        // Slide the sheen from off-left to off-right
                        // continuously. The mask traps it inside the
                        // content shape so the underlying gray blocks
                        // are what sweeps.
                        .offset(x: phase * geo.size.width)
                        .blendMode(.plusLighter)
                    }
                    .mask(content)
                )
                .onAppear {
                    withAnimation(
                        .linear(duration: duration)
                            .repeatForever(autoreverses: false),
                    ) {
                        phase = 1.5
                    }
                }
        }
    }
}
