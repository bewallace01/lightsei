// Phase 31.5.x: brand palette + reusable cosmic backgrounds.
//
// Lightsei's identity is a celestial dark theme. Surfaces lean on
// deep indigo / near-black backgrounds with a subtle starfield
// behind content. AccentColor (defined in the asset catalog) is the
// bright star against that dark sky.

import SwiftUI

enum LightseiTheme {
    // Deep cosmos: the base background color behind the starfield.
    // ~ #0B0A1F. Darker than Apple's systemBackground in dark mode
    // (which is #000) so we can stamp stars on top without crushing
    // them.
    static let cosmos = Color(red: 0.043, green: 0.039, blue: 0.122)

    // One zone above cosmos. Used for cards and content surfaces
    // that need to feel slightly raised from the night sky.
    // ~ #16132E.
    static let nebula = Color(red: 0.086, green: 0.075, blue: 0.18)

    // The accent. Indigo with a touch more brightness than
    // AssetCatalog's default. Use sparingly — accents are stars,
    // not the sky.
    static let starlight = Color(red: 0.55, green: 0.45, blue: 1.0)
}

// A static field of softly-glowing dots over a dark gradient. Cheap
// to render (no animation, no compute shaders) so it sits behind
// content without dropping frames on older devices.
//
// Drop into a ZStack as the bottom layer:
//
//   ZStack {
//     StarfieldBackground()
//     content
//   }
//   .ignoresSafeArea()
struct StarfieldBackground: View {
    // Density of stars per "tile" — a tile is a 400x800-ish chunk of
    // the canvas. 30 stars/tile reads as a quiet sky; 80+ starts to
    // feel busy.
    let density: Int

    // Seed for the deterministic random layout. Different seeds give
    // different constellations; same seed always gives the same.
    let seed: UInt64

    init(density: Int = 50, seed: UInt64 = 12345) {
        self.density = density
        self.seed = seed
    }

    var body: some View {
        GeometryReader { proxy in
            let size = proxy.size
            ZStack {
                // Gradient base: cosmos at top, slightly lighter at
                // bottom so the eye doesn't tire of a uniform black.
                LinearGradient(
                    colors: [
                        LightseiTheme.cosmos,
                        LightseiTheme.nebula,
                    ],
                    startPoint: .top,
                    endPoint: .bottom,
                )

                // Stars: a Canvas renders dots deterministically
                // from the seed. SwiftUI Canvas is fast for static
                // content and avoids the cost of a thousand
                // individual Circle views.
                Canvas { ctx, canvasSize in
                    var rng = SeededRandom(seed: seed)
                    let count = density * Int(
                        (canvasSize.width * canvasSize.height) / 320_000,
                    ).advanced(by: 1)
                    for _ in 0..<count {
                        let x = rng.next() * canvasSize.width
                        let y = rng.next() * canvasSize.height

                        // Brightness tiers — most stars are dim
                        // pinpricks, ~15% are sparkle stars with
                        // diagonal arms, ~3% are the headline stars
                        // with full halo.
                        let roll = rng.next()
                        let tier: StarTier
                        if roll < 0.03 { tier = .headline }
                        else if roll < 0.18 { tier = .sparkle }
                        else { tier = .dim }

                        // Subtle color variation: most white, some
                        // warm, some cool. Pulled from real stellar
                        // class colors so the sky doesn't look
                        // monochrome.
                        let colorRoll = rng.next()
                        let starColor: Color
                        if colorRoll < 0.08 {
                            // Warm K-type (orange)
                            starColor = Color(
                                red: 1.0, green: 0.85, blue: 0.7,
                            )
                        } else if colorRoll < 0.16 {
                            // Cool B-type (blue-white)
                            starColor = Color(
                                red: 0.85, green: 0.92, blue: 1.0,
                            )
                        } else {
                            starColor = .white
                        }

                        drawStar(
                            ctx: ctx,
                            at: CGPoint(x: x, y: y),
                            tier: tier,
                            color: starColor,
                            rng: &rng,
                        )
                    }
                }
                .frame(width: size.width, height: size.height)
            }
        }
    }
}

// Star size + decoration tier. `dim` = single pinprick. `sparkle`
// = brighter dot plus a 4-point cross (thin diagonal arms) for the
// characteristic "drawn star" glimmer. `headline` = sparkle + soft
// halo behind it.
private enum StarTier { case dim, sparkle, headline }

// Render one star into the Canvas at the given point. Centralizing
// this lets us add tier-specific decoration (halo, cross arms)
// without nesting it into the main loop.
private func drawStar(
    ctx: GraphicsContext,
    at point: CGPoint,
    tier: StarTier,
    color: Color,
    rng: inout SeededRandom,
) {
    switch tier {
    case .dim:
        let r: Double = 0.5 + rng.next() * 0.7
        let alpha: Double = 0.22 + rng.next() * 0.35
        let rect = CGRect(
            x: point.x - r, y: point.y - r,
            width: r * 2, height: r * 2,
        )
        ctx.fill(
            Path(ellipseIn: rect),
            with: .color(color.opacity(alpha)),
        )

    case .sparkle:
        let coreR: Double = 1.0 + rng.next() * 1.0
        let alpha: Double = 0.7 + rng.next() * 0.3
        // Soft glow behind the core for extra brightness.
        let glowR = coreR * 2.5
        ctx.fill(
            Path(ellipseIn: CGRect(
                x: point.x - glowR, y: point.y - glowR,
                width: glowR * 2, height: glowR * 2,
            )),
            with: .color(color.opacity(alpha * 0.15)),
        )
        // Thin 4-point cross — horizontal + vertical thin rects.
        let armLen = coreR * 4.0
        let armWidth = max(0.5, coreR * 0.35)
        ctx.fill(
            Path(CGRect(
                x: point.x - armLen / 2, y: point.y - armWidth / 2,
                width: armLen, height: armWidth,
            )),
            with: .color(color.opacity(alpha * 0.85)),
        )
        ctx.fill(
            Path(CGRect(
                x: point.x - armWidth / 2, y: point.y - armLen / 2,
                width: armWidth, height: armLen,
            )),
            with: .color(color.opacity(alpha * 0.85)),
        )
        // Bright core circle on top.
        ctx.fill(
            Path(ellipseIn: CGRect(
                x: point.x - coreR, y: point.y - coreR,
                width: coreR * 2, height: coreR * 2,
            )),
            with: .color(color.opacity(alpha)),
        )

    case .headline:
        let coreR: Double = 1.8 + rng.next() * 1.2
        let alpha: Double = 0.9 + rng.next() * 0.1
        // Big soft halo — concentric rings fading out.
        for ringMul in [4.5, 3.0] {
            let rr = coreR * ringMul
            ctx.fill(
                Path(ellipseIn: CGRect(
                    x: point.x - rr, y: point.y - rr,
                    width: rr * 2, height: rr * 2,
                )),
                with: .color(color.opacity(0.10)),
            )
        }
        // Cross arms a little longer than .sparkle.
        let armLen = coreR * 5.5
        let armWidth = max(0.6, coreR * 0.3)
        ctx.fill(
            Path(CGRect(
                x: point.x - armLen / 2, y: point.y - armWidth / 2,
                width: armLen, height: armWidth,
            )),
            with: .color(color.opacity(alpha * 0.9)),
        )
        ctx.fill(
            Path(CGRect(
                x: point.x - armWidth / 2, y: point.y - armLen / 2,
                width: armWidth, height: armLen,
            )),
            with: .color(color.opacity(alpha * 0.9)),
        )
        ctx.fill(
            Path(ellipseIn: CGRect(
                x: point.x - coreR, y: point.y - coreR,
                width: coreR * 2, height: coreR * 2,
            )),
            with: .color(color.opacity(alpha)),
        )
    }
}

// Tiny deterministic PRNG so the starfield is identical across
// runs (no flicker on re-render). Linear-congruential, plenty good
// for picking pixel positions for ~thousands of stars.
private struct SeededRandom {
    var state: UInt64
    init(seed: UInt64) { self.state = seed == 0 ? 1 : seed }
    mutating func next() -> Double {
        state = state &* 6364136223846793005 &+ 1442695040888963407
        return Double(state >> 11) / Double(1 << 53)
    }
}
