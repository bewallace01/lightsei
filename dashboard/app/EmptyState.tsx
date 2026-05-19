"use client";

import Link from "next/link";

// Phase 18.2: shared empty-state shape used across home / agents /
// zones / runs (and any future page that wants a useful blank state).
// Goal: when a non-technical user lands on an empty surface, they
// should see one obvious next action — not a placeholder table.
//
// Shape: optional headline, body, a primary CTA (highlighted button),
// and an optional secondary CTA (subdued link). All CTAs are either
// in-app Link routes or external/raw <a> hrefs.

type CTAProps = {
  href: string;
  label: string;
  // External or raw <a>? Defaults to in-app Link (next/link).
  external?: boolean;
};

export type EmptyStateProps = {
  // Headline — bold, single sentence.
  title: string;
  // Body — 1-3 sentences of context. Keep it short.
  body?: React.ReactNode;
  // Primary CTA — highlighted button. The next-best action.
  primary?: CTAProps;
  // Secondary CTA — subdued link to a related action.
  secondary?: CTAProps;
  // Pad differently for hero vs in-table empty states.
  // 'lg' (default) = standalone empty surface; 'sm' = inline within a
  // table or section where the page chrome is doing the framing.
  size?: "lg" | "sm";
  // Optional small icon/illustration above the title. Pass a React
  // node (typically an SVG). Default = no icon.
  icon?: React.ReactNode;
};

function CTAButton({ href, label, external }: CTAProps) {
  const className =
    "px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline transition-colors";
  if (external) {
    return (
      <a href={href} className={className} target="_blank" rel="noopener noreferrer">
        {label}
      </a>
    );
  }
  return (
    <Link href={href} className={className}>
      {label}
    </Link>
  );
}

function CTASecondary({ href, label, external }: CTAProps) {
  const className =
    "px-4 py-2 border border-gray-300 text-gray-700 rounded-md text-sm font-medium hover:bg-gray-50 no-underline transition-colors";
  if (external) {
    return (
      <a href={href} className={className} target="_blank" rel="noopener noreferrer">
        {label}
      </a>
    );
  }
  return (
    <Link href={href} className={className}>
      {label}
    </Link>
  );
}

export default function EmptyState({
  title,
  body,
  primary,
  secondary,
  size = "lg",
  icon,
}: EmptyStateProps) {
  const padding = size === "lg" ? "p-10" : "p-6";
  return (
    <div
      className={`border border-dashed border-gray-200 rounded-lg ${padding} text-center`}
    >
      {icon && (
        <div className="flex justify-center mb-3 text-gray-400">{icon}</div>
      )}
      <div className="text-gray-700 font-medium mb-2">{title}</div>
      {body && <p className="text-sm text-gray-500 mb-4 max-w-xl mx-auto">{body}</p>}
      {(primary || secondary) && (
        <div className="flex items-center justify-center gap-3 flex-wrap">
          {primary && <CTAButton {...primary} />}
          {secondary && <CTASecondary {...secondary} />}
        </div>
      )}
    </div>
  );
}
