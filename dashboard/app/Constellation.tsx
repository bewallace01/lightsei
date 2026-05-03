"use client";

/**
 * Phase 11B.3: the home-page constellation map.
 *
 * Polaris is rendered as a sun at the canvas center; other agents are
 * smaller cool-tinted stars whose tint is stable per agent name.
 * Dispatch edges (when 11.2 lands them) are quadratic Bezier curves
 * whose control points bias toward the canvas center, so the
 * constellation reads as a solar system held together by Polaris's
 * gravity rather than a random graph.
 *
 * Position is fully deterministic — no force-directed layout — so the
 * map doesn't shift between page loads or polling ticks. Hash-of-name
 * picks the angle, role picks the radius, with a post-hoc collision
 * bump so two non-Polaris agents that happen to land within 80px of
 * each other get fanned out.
 *
 * Animations (twinkle on event arrival, edge pulse on dispatch arrival)
 * are CSS-driven via classes added/removed by the React component on
 * each polling tick; reduced-motion users get a static snapshot.
 *
 * The component is intentionally self-contained — no external charting
 * library, no canvas, just SVG + Tailwind + a couple of `useEffect`s.
 * Single file because the moment of truth here is the visual; if it
 * looks great as one tight unit, animation is just polish; if it
 * doesn't, splitting it across files won't help.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ConstellationAgent,
  ConstellationData,
  ConstellationEdge,
  fetchConstellation,
  UnauthorizedError,
} from "./api";

// ---- Layout constants. ---- //

const VB_W = 1000;
const VB_H = 480;
const CENTER = { x: VB_W / 2, y: VB_H / 2 };

// Orbital radius by role tier.
const RADIUS_BY_ROLE: Record<ConstellationAgent["role"], number> = {
  orchestrator: 0, // dead center
  executor: 150,
  specialist: 200,
  notifier: 250,
};

// Per-role angle offset so executors and notifiers don't pile up at
// matching angles (otherwise a notifier with the same hash as an
// executor would draw directly behind it).
const ANGLE_OFFSET_BY_ROLE: Record<ConstellationAgent["role"], number> = {
  orchestrator: 0,
  executor: 0,
  specialist: Math.PI / 6, // 30° offset
  notifier: Math.PI / 4, //   45° offset
};

// ---- Cool palette for non-orchestrator agents. ---- //

// Tint is hash(name) % palette.length so each agent is recognizable
// at a glance even when status colors collide. Pure white is reserved
// for the sun, amber is reserved for Polaris's gradient — everything
// else is the cool half of the color wheel at the lightest tier
// (*-200) so each star reads as "white with a hint of color" the way
// real stars look against a dark sky, rather than "saturated UI dot."
// Spread across the wheel so adjacent palette indices still feel
// distinct.
const PALETTE = [
  "#bfdbfe", // blue-200
  "#a5f3fc", // cyan-200
  "#99f6e4", // teal-200
  "#a7f3d0", // emerald-200
  "#ddd6fe", // violet-200
  "#f5d0fe", // fuchsia-200
  "#fecdd3", // rose-200
  "#fbcfe8", // pink-200
];

// ---- Hash + position helpers. ---- //

// FNV-1a 32-bit. Stable across page loads, no crypto needed; the only
// requirement is "two agents with different names get different angles."
function hash32(s: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193);
  }
  return h >>> 0;
}

function tintForAgent(name: string): string {
  return PALETTE[hash32(name) % PALETTE.length];
}

function positionFor(
  agent: ConstellationAgent,
  others: ConstellationAgent[],
): { x: number; y: number } {
  if (agent.role === "orchestrator") {
    return { x: CENTER.x, y: CENTER.y };
  }
  const radius = RADIUS_BY_ROLE[agent.role] ?? RADIUS_BY_ROLE.executor;
  const angle =
    (hash32(agent.name) / 0xffffffff) * Math.PI * 2 +
    ANGLE_OFFSET_BY_ROLE[agent.role];
  let x = CENTER.x + Math.cos(angle) * radius;
  let y = CENTER.y + Math.sin(angle) * radius;
  // Post-hoc collision bump: if any prior non-orchestrator agent
  // landed within 80px, fan this one out by +30 on its radius.
  // Cheap O(n²) but n is tiny.
  for (const other of others) {
    if (other.name === agent.name || other.role === "orchestrator") continue;
    const oRadius = RADIUS_BY_ROLE[other.role] ?? RADIUS_BY_ROLE.executor;
    const oAngle =
      (hash32(other.name) / 0xffffffff) * Math.PI * 2 +
      ANGLE_OFFSET_BY_ROLE[other.role];
    const ox = CENTER.x + Math.cos(oAngle) * oRadius;
    const oy = CENTER.y + Math.sin(oAngle) * oRadius;
    const d = Math.hypot(x - ox, y - oy);
    if (d < 80) {
      x = CENTER.x + Math.cos(angle) * (radius + 30);
      y = CENTER.y + Math.sin(angle) * (radius + 30);
      break;
    }
  }
  return { x, y };
}

// Star size: small at idle, grows with activity, capped so a runaway
// agent doesn't eat the canvas. Polaris is a fixed override because
// its size encodes role, not volume. Sizes here are the OUTER point
// distance from center for the sparkle path.
function starSize(agent: ConstellationAgent): number {
  if (agent.role === "orchestrator") return 28;
  const base = 7 + Math.sqrt(agent.runs_24h) * 1.5;
  return Math.min(16, Math.max(7, base));
}

/**
 * 4-point sparkle path matching the Lightsei logo's geometry. `outer`
 * is the distance from center to the four cardinal points; `inner`
 * controls how spiky vs round the sparkle is — lower inner ratio =
 * more pointy. The logo uses ~0.19, which reads as proper celestial
 * sparkle; we use a slightly higher ratio (0.30) for the small agent
 * stars so they stay legible at 7–16px.
 */
function sparklePath(
  cx: number,
  cy: number,
  outer: number,
  innerRatio: number,
): string {
  const inner = outer * innerRatio;
  // Inner vertex offset from center along each diagonal. Use sqrt(2)/2
  // so the inner points sit on a circle of radius `inner`.
  const i = inner / Math.SQRT2;
  return [
    `M${cx},${cy - outer}`, // top
    `L${cx + i},${cy - i}`, // inner top-right
    `L${cx + outer},${cy}`, // right
    `L${cx + i},${cy + i}`, // inner bottom-right
    `L${cx},${cy + outer}`, // bottom
    `L${cx - i},${cy + i}`, // inner bottom-left
    `L${cx - outer},${cy}`, // left
    `L${cx - i},${cy - i}`, // inner top-left
    "Z",
  ].join(" ");
}

/**
 * 8-point compass-rose star — 4 long cardinal rays (N/E/S/W) plus 4
 * shorter secondary rays at the diagonals, with concave inner vertices
 * between every pair so each ray reads as a tapered shape rather than
 * a thin line. Used for Polaris specifically; the small agent stars
 * stay as the simpler 4-point sparkles to keep the visual hierarchy.
 *
 * outer:          distance from center to the four cardinal points (long rays)
 * outerSecondary: distance from center to the diagonal points (short rays)
 * inner:          distance to the concave inner vertices (smaller = pointier)
 *
 * Vertex order: N, NE-inner, NE-outer, E-inner, E, SE-inner, SE-outer,
 * S-inner, S, SW-inner, SW-outer, W-inner, W, NW-inner, NW-outer, N-inner.
 */
function compassStarPath(
  cx: number,
  cy: number,
  outer: number,
  outerSecondary: number,
  inner: number,
): string {
  const pts: { x: number; y: number }[] = [];
  for (let i = 0; i < 16; i++) {
    // Start at the top (12 o'clock) and walk clockwise. Index mod 4:
    //   0  -> cardinal point (N/E/S/W) — long ray
    //   1  -> inner concave vertex
    //   2  -> diagonal point — short ray
    //   3  -> inner concave vertex
    const angle = (i * Math.PI) / 8 - Math.PI / 2;
    let r: number;
    if (i % 4 === 0) r = outer;
    else if (i % 4 === 2) r = outerSecondary;
    else r = inner;
    pts.push({
      x: cx + Math.cos(angle) * r,
      y: cy + Math.sin(angle) * r,
    });
  }
  return [
    `M${pts[0].x},${pts[0].y}`,
    ...pts.slice(1).map((p) => `L${p.x},${p.y}`),
    "Z",
  ].join(" ");
}

// Bezier control point for an edge from `from` to `to`. The control
// point is the midpoint offset perpendicular to the line by a bend
// amount that scales with distance, biased toward the canvas center
// so lines curve "inward" through Polaris's gravity. For multi-edges
// between the same pair (rare in v1, common later), the second edge
// could call this with `flipBias: true` so they don't overlap.
function bezierControl(
  from: { x: number; y: number },
  to: { x: number; y: number },
  flipBias = false,
): { x: number; y: number } {
  const mx = (from.x + to.x) / 2;
  const my = (from.y + to.y) / 2;
  const dx = to.x - from.x;
  const dy = to.y - from.y;
  const len = Math.hypot(dx, dy) || 1;
  const px = -dy / len;
  const py = dx / len;
  const bend = Math.min(36, Math.max(10, len / 8));
  const c1 = { x: mx + px * bend, y: my + py * bend };
  const c2 = { x: mx - px * bend, y: my - py * bend };
  const d1 =
    (c1.x - CENTER.x) ** 2 + (c1.y - CENTER.y) ** 2;
  const d2 =
    (c2.x - CENTER.x) ** 2 + (c2.y - CENTER.y) ** 2;
  // Pick whichever bends toward the canvas center, unless flipped.
  if (flipBias) return d1 < d2 ? c2 : c1;
  return d1 < d2 ? c1 : c2;
}

// ---- Component ---- //

type AgentPlacement = {
  agent: ConstellationAgent;
  pos: { x: number; y: number };
  size: number;
  tint: string;
};

export default function Constellation() {
  const [data, setData] = useState<ConstellationData | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);
  const lastEventAt = useRef<Map<string, string | null>>(new Map());
  const [recentlyTwinkled, setRecentlyTwinkled] = useState<Set<string>>(
    () => new Set(),
  );

  // 5s poll, paused when the tab is hidden so we don't burn API while
  // the user is in another window.
  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | null = null;

    const tick = async () => {
      if (document.visibilityState === "hidden") {
        if (alive) timer = setTimeout(tick, 5000);
        return;
      }
      try {
        const fresh = await fetchConstellation();
        if (!alive) return;
        // Detect new events per agent: if any agent's last_event_at is
        // strictly greater than what we saw last poll, twinkle that star.
        const newlyTwinkled = new Set<string>();
        for (const a of fresh.agents) {
          const prev = lastEventAt.current.get(a.name) ?? null;
          if (
            a.last_event_at !== null &&
            (prev === null || a.last_event_at > prev)
          ) {
            newlyTwinkled.add(a.name);
          }
          lastEventAt.current.set(a.name, a.last_event_at);
        }
        if (newlyTwinkled.size > 0) {
          setRecentlyTwinkled(newlyTwinkled);
          // Clear after the keyframe duration so the class can be re-added
          // on the next event.
          setTimeout(() => {
            if (!alive) return;
            setRecentlyTwinkled(new Set());
          }, 700);
        }
        setData(fresh);
        setError(null);
      } catch (e) {
        if (!alive) return;
        if (e instanceof UnauthorizedError) {
          // Surface as null state — the parent page handles the redirect.
          return;
        }
        setError(String(e));
      } finally {
        if (alive) timer = setTimeout(tick, 5000);
      }
    };

    tick();
    const onVis = () => {
      if (document.visibilityState === "visible" && alive) {
        // Resume immediately when the user comes back.
        if (timer) clearTimeout(timer);
        tick();
      }
    };
    document.addEventListener("visibilitychange", onVis);

    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, []);

  const placements: AgentPlacement[] = useMemo(() => {
    if (!data) return [];
    return data.agents.map((a, _i, all) => ({
      agent: a,
      pos: positionFor(a, all),
      size: starSize(a),
      tint: tintForAgent(a.name),
    }));
  }, [data]);

  const placementByName = useMemo(() => {
    const m = new Map<string, AgentPlacement>();
    for (const p of placements) m.set(p.agent.name, p);
    return m;
  }, [placements]);

  const hoveredAgent = hovered
    ? placementByName.get(hovered)?.agent ?? null
    : null;
  const hoveredPos = hovered ? placementByName.get(hovered)?.pos ?? null : null;

  return (
    <div className="relative">
      <style jsx>{`
        @keyframes star-twinkle {
          0% {
            transform: scale(1);
            opacity: 1;
          }
          50% {
            transform: scale(1.15);
            opacity: 0.85;
          }
          100% {
            transform: scale(1);
            opacity: 1;
          }
        }
        @keyframes sun-twinkle {
          0% {
            transform: scale(1);
          }
          50% {
            transform: scale(1.06);
          }
          100% {
            transform: scale(1);
          }
        }
        .agent-star.twinkle .star-core {
          animation: star-twinkle 600ms ease-out 1;
          transform-origin: center;
          transform-box: fill-box;
        }
        .agent-star.sun.twinkle .star-core {
          animation: sun-twinkle 600ms ease-out 1;
        }
        @media (prefers-reduced-motion: reduce) {
          .agent-star.twinkle .star-core,
          .agent-star.sun.twinkle .star-core {
            animation: none;
          }
        }
      `}</style>

      <svg
        viewBox={`0 0 ${VB_W} ${VB_H}`}
        className="w-full rounded-lg border border-indigo-900/50 shadow-lg shadow-indigo-950/30"
        style={{ aspectRatio: `${VB_W} / ${VB_H}` }}
        role="img"
        aria-label="Constellation map of agents"
      >
        <defs>
          {/* Solid radial-into-linear gradient so the canvas reads as
              a real night sky with a faint warmth in the center where
              Polaris lives. Matches the visual depth of the /polaris
              hero so the two screens feel like one design family. */}
          <linearGradient id="sky-grad" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="#020617" />
            <stop offset="50%" stopColor="#1e1b4b" />
            <stop offset="100%" stopColor="#0f172a" />
          </linearGradient>
          <radialGradient id="sky-warmth" cx="50%" cy="50%" r="55%">
            <stop offset="0%" stopColor="rgba(251, 191, 36, 0.06)" />
            <stop offset="60%" stopColor="rgba(251, 191, 36, 0)" />
          </radialGradient>
          {/* Sun glow — multi-stop radial that goes from white-hot at
              the very center, through cream + amber, fading smoothly
              to nothing. Real bright stars don't have a hard amber
              boundary; the warmth bleeds into the surrounding sky. */}
          <radialGradient id="sun-glow" cx="50%" cy="50%" r="50%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="1" />
            <stop offset="8%" stopColor="#fff7ed" stopOpacity="0.95" />
            <stop offset="22%" stopColor="#fde68a" stopOpacity="0.65" />
            <stop offset="45%" stopColor="#fbbf24" stopOpacity="0.30" />
            <stop offset="75%" stopColor="#f59e0b" stopOpacity="0.10" />
            <stop offset="100%" stopColor="#f59e0b" stopOpacity="0" />
          </radialGradient>
          {/* Diffraction-spike gradients: bright at the middle, fading
              to transparent at both ends. Drawn as thin rects that get
              blurred slightly to soften the edges. */}
          <linearGradient id="ray-h" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0" />
            <stop offset="35%" stopColor="#fef3c7" stopOpacity="0.55" />
            <stop offset="50%" stopColor="#ffffff" stopOpacity="0.95" />
            <stop offset="65%" stopColor="#fef3c7" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
          </linearGradient>
          <linearGradient id="ray-v" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0" />
            <stop offset="35%" stopColor="#fef3c7" stopOpacity="0.55" />
            <stop offset="50%" stopColor="#ffffff" stopOpacity="0.95" />
            <stop offset="65%" stopColor="#fef3c7" stopOpacity="0.55" />
            <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
          </linearGradient>
          <filter id="halo-blur" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="4" />
          </filter>
          <filter id="halo-blur-strong" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="8" />
          </filter>
          <filter id="ray-blur" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="0.6" />
          </filter>
        </defs>

        {/* Painted sky background — gradient first, then a soft amber
            warmth in the center so Polaris looks like it's actually
            radiating heat into the canvas. */}
        <rect width={VB_W} height={VB_H} fill="url(#sky-grad)" />
        <rect width={VB_W} height={VB_H} fill="url(#sky-warmth)" />

        {/* Faint background star dots for atmosphere. Static positions
            with a mix of sizes + opacities so the field reads as depth
            rather than a regular grid. */}
        <g aria-hidden="true">
          {[
            { x: 80, y: 60, r: 1.2, o: 0.7 },
            { x: 220, y: 110, r: 0.8, o: 0.5 },
            { x: 380, y: 80, r: 1.4, o: 0.8 },
            { x: 540, y: 50, r: 0.8, o: 0.5 },
            { x: 700, y: 130, r: 1.2, o: 0.7 },
            { x: 880, y: 70, r: 1, o: 0.6 },
            { x: 940, y: 220, r: 1.4, o: 0.5 },
            { x: 60, y: 320, r: 0.8, o: 0.5 },
            { x: 200, y: 400, r: 1.2, o: 0.5 },
            { x: 360, y: 440, r: 1, o: 0.6 },
            { x: 520, y: 410, r: 0.8, o: 0.5 },
            { x: 720, y: 380, r: 1.2, o: 0.7 },
            { x: 860, y: 420, r: 0.8, o: 0.5 },
            { x: 920, y: 350, r: 1, o: 0.6 },
            { x: 130, y: 200, r: 0.8, o: 0.5 },
            { x: 870, y: 280, r: 1.4, o: 0.8 },
            { x: 290, y: 240, r: 0.7, o: 0.45 },
            { x: 660, y: 260, r: 0.7, o: 0.45 },
            { x: 480, y: 90, r: 0.9, o: 0.55 },
            { x: 460, y: 410, r: 0.9, o: 0.55 },
          ].map((d, i) => (
            <circle
              key={i}
              cx={d.x}
              cy={d.y}
              r={d.r}
              fill="white"
              opacity={d.o}
            />
          ))}
        </g>

        {/* Edges first so stars sit on top. */}
        <g aria-hidden="true">
          {data?.edges.map((edge: ConstellationEdge, i: number) => {
            const from = placementByName.get(edge.from)?.pos;
            const to = placementByName.get(edge.to)?.pos;
            if (!from || !to) return null;
            const cp = bezierControl(from, to);
            // Stroke opacity: log scale, 1 dispatch = 0.30, 100 = 0.70.
            const opacity =
              0.3 +
              Math.min(0.4, Math.log10((edge.count_24h || 1) + 1) * 0.2);
            const width =
              Math.min(4, 1 + Math.log10((edge.count_24h || 1) + 1));
            return (
              <path
                key={`${edge.from}-${edge.to}-${i}`}
                d={`M${from.x},${from.y} Q${cp.x},${cp.y} ${to.x},${to.y}`}
                fill="none"
                stroke="rgb(199 210 254)"
                strokeOpacity={opacity}
                strokeWidth={width}
                strokeLinecap="round"
              />
            );
          })}
        </g>

        {/* Agent stars. */}
        <g>
          {placements.map(({ agent, pos, size, tint }) => {
            const isSun = agent.role === "orchestrator";
            const stale = agent.status === "stale";
            const stopped = agent.status === "stopped";
            const twinkle = recentlyTwinkled.has(agent.name);
            return (
              <g
                key={agent.name}
                className={`agent-star ${isSun ? "sun" : ""} ${
                  twinkle ? "twinkle" : ""
                }`}
                tabIndex={0}
                role="button"
                aria-label={`${agent.name}: ${agent.role}, status ${agent.status}, ${agent.runs_24h} runs in last 24h`}
                style={{ cursor: "pointer", outline: "none" }}
                onMouseEnter={() => setHovered(agent.name)}
                onMouseLeave={() => setHovered(null)}
                onFocus={() => setHovered(agent.name)}
                onBlur={() => setHovered(null)}
                onClick={() => {
                  window.location.href = `/agents/${encodeURIComponent(agent.name)}`;
                }}
              >
                {isSun ? (
                  // The sun: 8-point compass star (long cardinal rays,
                  // shorter diagonals, all tapered) on top of a warm
                  // multi-stop halo. The halo gives sun-warmth without
                  // a hard boundary; the star shape gives it
                  // structure. .star-core wraps both so the twinkle
                  // animation scales them together.
                  <g className="star-core">
                    {/* Wide warm halo — multi-stop radial gradient
                        bleeds smoothly from white-hot center into the
                        surrounding sky. Sized so the longer cardinal
                        rays still appear to "shine" through the glow
                        rather than poking past it. */}
                    <circle
                      cx={pos.x}
                      cy={pos.y}
                      r={size * 3.2}
                      fill="url(#sun-glow)"
                      filter="url(#halo-blur)"
                    />
                    {/* The star: 8-point compass shape in amber, with
                        a faint warm rim so the points read crisp
                        against the halo. Cardinal rays at 1.5× size
                        give the dramatic proportions the reference
                        compass stars use; outerSecondary at 0.50 keeps
                        the diagonal rays substantial; inner at 0.18
                        gives each ray a thicker tapered base instead
                        of a hairline tip. */}
                    <path
                      d={compassStarPath(
                        pos.x,
                        pos.y,
                        size * 1.5,
                        size * 0.50,
                        size * 0.18,
                      )}
                      fill="url(#sun-glow)"
                      stroke="#fbbf24"
                      strokeOpacity={0.6}
                      strokeWidth={0.6}
                      strokeLinejoin="miter"
                    />
                    {/* Tight bright core — a small opaque white pixel
                        at the very center so there's a definite "the
                        thing is THERE" anchor. The compass star above
                        is structure; this is the bulb at the middle. */}
                    <circle
                      cx={pos.x}
                      cy={pos.y}
                      r={size * 0.13}
                      fill="#ffffff"
                    />
                  </g>
                ) : (
                  <>
                    {/* Soft circular glow halo. Always rendered in
                        the agent's per-name tint so each star keeps
                        its identity even when it isn't actively
                        running — opacity is what conveys status, not
                        color. Round so it reads as the star's
                        atmosphere rather than repeating the sparkle
                        silhouette. */}
                    <circle
                      cx={pos.x}
                      cy={pos.y}
                      r={size + 5}
                      fill={tint}
                      opacity={
                        stopped ? 0.10 : stale ? 0.14 : 0.22
                      }
                      filter="url(#halo-blur)"
                    />
                    {/* 4-point sparkle in the agent's tint. 0.30 inner
                        ratio keeps small stars legible at 7–16px
                        without losing their pointed silhouette.
                        Stopped/stale dim the opacity but keep the
                        tint so atlas's hue stays visibly distinct
                        from hermes's even when neither has a
                        heartbeat. No white outline — the dark canvas
                        is enough contrast on its own and the outline
                        was over-brightening the pastels. */}
                    <path
                      className="star-core"
                      d={sparklePath(pos.x, pos.y, size, 0.30)}
                      fill={tint}
                      opacity={
                        stopped ? 0.55 : stale ? 0.70 : 0.92
                      }
                      strokeLinejoin="round"
                    />
                  </>
                )}
                {/* Label. */}
                <text
                  x={pos.x}
                  y={pos.y + size + 14}
                  textAnchor="middle"
                  className="font-mono pointer-events-none"
                  fill={isSun ? "rgb(254 240 138)" : "rgb(199 210 254)"}
                  opacity={isSun ? 0.95 : 0.7}
                  fontSize={isSun ? 12 : 11}
                  fontWeight={isSun ? 600 : 400}
                >
                  {agent.name}
                </text>
              </g>
            );
          })}
        </g>

        {/* Empty state — drawn last so it sits over a faint dotted teaser. */}
        {data && data.agents.length === 0 && !error && (
          <g aria-label="No agents yet">
            {/* Faint constellation outline as a teaser. */}
            <g
              stroke="rgb(165 180 252)"
              strokeOpacity={0.30}
              strokeDasharray="3 5"
              strokeWidth={1}
              fill="none"
            >
              <line x1={500} y1={240} x2={420} y2={180} />
              <line x1={500} y1={240} x2={580} y2={180} />
              <line x1={500} y1={240} x2={420} y2={300} />
              <line x1={500} y1={240} x2={580} y2={300} />
            </g>
            <g fill="rgb(199 210 254)" fillOpacity={0.45}>
              <path d={sparklePath(500, 240, 8, 0.19)} />
              <path d={sparklePath(420, 180, 5, 0.30)} />
              <path d={sparklePath(580, 180, 5, 0.30)} />
              <path d={sparklePath(420, 300, 5, 0.30)} />
              <path d={sparklePath(580, 300, 5, 0.30)} />
            </g>
            <text
              x={500}
              y={380}
              textAnchor="middle"
              className="font-serif"
              fill="rgb(224 231 255)"
              opacity={0.95}
              fontSize={22}
            >
              Sky empty.
            </text>
            <text
              x={500}
              y={408}
              textAnchor="middle"
              fill="rgb(165 180 252)"
              opacity={0.75}
              fontSize={13}
            >
              Deploy your first agent →
            </text>
          </g>
        )}
      </svg>

      {/* Hover tooltip (HTML, not SVG, for cleaner styling). */}
      {hoveredAgent && hoveredPos && (
        <Tooltip agent={hoveredAgent} pos={hoveredPos} />
      )}

      {error && (
        <div className="absolute top-2 right-2 px-2 py-1 rounded bg-red-900/60 text-red-100 text-[11px]">
          {error}
        </div>
      )}
    </div>
  );
}

// ---- Hover tooltip ---- //

function Tooltip({
  agent,
  pos,
}: {
  agent: ConstellationAgent;
  pos: { x: number; y: number };
}) {
  // Position the tooltip in screen coordinates derived from the SVG
  // viewBox; the SVG is responsive so we use percent offsets.
  const leftPct = (pos.x / VB_W) * 100;
  const topPct = (pos.y / VB_H) * 100;
  const statusLabel = {
    active: "active",
    stale: "stale",
    stopped: "stopped",
  }[agent.status];

  return (
    <div
      className="absolute pointer-events-none rounded-md border border-indigo-900/60 bg-slate-900/95 backdrop-blur px-3 py-2 text-xs text-indigo-100 shadow-lg z-10"
      style={{
        left: `${leftPct}%`,
        top: `calc(${topPct}% + 28px)`,
        transform: "translate(-50%, 0)",
        minWidth: 180,
        maxWidth: 240,
      }}
    >
      <div className="flex items-center justify-between gap-3 mb-1">
        <span className="font-mono font-medium text-indigo-50">
          {agent.name}
        </span>
        <span className="text-[10px] uppercase tracking-wider text-indigo-300">
          {agent.role}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-0.5 text-[11px]">
        <span className="text-indigo-300">status</span>
        <span className="font-mono">{statusLabel}</span>
        {agent.model && (
          <>
            <span className="text-indigo-300">model</span>
            <span className="font-mono truncate">{agent.model}</span>
          </>
        )}
        <span className="text-indigo-300">runs 24h</span>
        <span className="font-mono">{agent.runs_24h}</span>
        <span className="text-indigo-300">cost 24h</span>
        <span className="font-mono">
          ${agent.cost_24h_usd.toFixed(4)}
        </span>
      </div>
      <div className="mt-1 text-[10px] text-indigo-400">
        click to view →
      </div>
    </div>
  );
}
