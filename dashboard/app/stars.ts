/**
 * Shared star rendering helpers used across the home page —
 * Constellation.tsx for the marquee map and CostPanel.tsx (Phase
 * 11B.4) for the small star icons in the per-agent breakdown.
 *
 * Single source of truth for: hash → palette index, palette colors,
 * the 4-point sparkle SVG path. Adding a new place that wants
 * agent-shaped stars (the dispatch chain view, the recent activity
 * timeline, etc.) imports from here so an Atlas star is the same
 * violet sparkle everywhere it appears.
 */

// FNV-1a 32-bit. Stable across page loads, no crypto needed; the
// only requirement is "two agents with different names get different
// palette indices."
export function hash32(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

// Cool half of the color wheel at the lightest tier (*-200) so each
// star reads as "white with a hint of color" the way real stars look
// against a dark sky. Spread out across the wheel so adjacent palette
// indices stay visibly distinct.
export const STAR_PALETTE = [
  "#bfdbfe", // blue-200
  "#a5f3fc", // cyan-200
  "#99f6e4", // teal-200
  "#a7f3d0", // emerald-200
  "#ddd6fe", // violet-200
  "#f5d0fe", // fuchsia-200
  "#fecdd3", // rose-200
  "#fbcfe8", // pink-200
];

export function tintForAgent(name: string): string {
  return STAR_PALETTE[hash32(name) % STAR_PALETTE.length];
}

/**
 * 4-point sparkle path matching the Lightsei logo's geometry.
 * `outer` = distance from center to the four cardinal points;
 * `innerRatio` controls spikiness (lower = more pointy). The Lightsei
 * logo uses ~0.19; the constellation map's small stars use 0.30 for
 * legibility at 7–16px.
 */
export function sparklePath(
  cx: number,
  cy: number,
  outer: number,
  innerRatio = 0.3,
): string {
  const inner = outer * innerRatio;
  const i = inner / Math.SQRT2;
  return [
    `M${cx},${cy - outer}`,
    `L${cx + i},${cy - i}`,
    `L${cx + outer},${cy}`,
    `L${cx + i},${cy + i}`,
    `L${cx},${cy + outer}`,
    `L${cx - i},${cy + i}`,
    `L${cx - outer},${cy}`,
    `L${cx - i},${cy - i}`,
    "Z",
  ].join(" ");
}
