"use client";

// Shared trust-zone UI primitives. Used by /agents (column chip),
// /agents/{name} (header chip + editor), /zones (lane buckets), and
// the constellation map (node coloring).
//
// Keep the color mapping in one place so a change propagates
// everywhere consistently — non-technical users rely on the visual
// pattern (green = safe, red = sensitive) for the trust-zone story
// to land at a glance.

import type { SensitivityLevel } from "./api";

export const SENSITIVITY_TONE: Record<
  SensitivityLevel,
  { chip: string; node: string; lane: string; label: string }
> = {
  // Open team / research bots. Outbound HTTP allowed by default;
  // no PII expected. Green = "safe to send anywhere."
  public: {
    chip: "bg-green-100 text-green-800",
    node: "#10b981", // emerald-500
    lane: "bg-green-50 border-green-200",
    label: "public",
  },
  // Default workspace zone. Internal tools, no PII boundary, but
  // not internet-exposed by default. Amber tone signals "ordinary
  // work, no special handling needed."
  internal: {
    chip: "bg-amber-100 text-amber-800",
    node: "#f59e0b", // amber-500
    lane: "bg-amber-50 border-amber-200",
    label: "internal",
  },
  // Sensitive: customer-aware but not PII-tagged. Orange escalates
  // the visual weight before the user hits the red zone.
  sensitive: {
    chip: "bg-orange-100 text-orange-800",
    node: "#f97316", // orange-500
    lane: "bg-orange-50 border-orange-200",
    label: "sensitive",
  },
  // The compliance zone. Red is deliberately attention-grabbing —
  // anything touching a pii bot should be visually unmissable on
  // the dashboard so misconfigurations get noticed.
  pii: {
    chip: "bg-red-100 text-red-800",
    node: "#ef4444", // red-500
    lane: "bg-red-50 border-red-200",
    label: "pii",
  },
};

// Compact chip rendered on /agents (next to Quality), on
// /agents/{name} header, and inside the missing-secrets panel any
// time we need to show "what zone does this agent live in."
export function SensitivityChip({
  level,
  size = "sm",
}: {
  level: SensitivityLevel;
  size?: "sm" | "md";
}): JSX.Element {
  const tone = SENSITIVITY_TONE[level] ?? SENSITIVITY_TONE.internal;
  const sizing =
    size === "md"
      ? "px-2.5 py-1 text-xs"
      : "px-2 py-0.5 text-[11px]";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full font-medium ${tone.chip} ${sizing}`}
      title={`trust zone: ${tone.label}`}
    >
      <span
        className="inline-block w-1.5 h-1.5 rounded-full"
        style={{ backgroundColor: tone.node }}
      />
      {tone.label}
    </span>
  );
}
