"use client";

// Phase 18.7: inline help affordance for technical terms.
//
// Render <HelpTip term="sensitivity_zone" /> next to a label (or wrap a
// label with <HelpTip term="..." inline>label text</HelpTip>) and the
// user gets a small "?" badge they can hover for a 1-2 line explanation
// drawn from glossary.ts. Optional docs link surfaces inside the tooltip.
//
// CSS-only show/hide via Tailwind's group-hover variant — no JS state.
// Works on touch via :focus-within when the icon is keyboard-focused.

import Link from "next/link";
import { GLOSSARY, GlossaryKey } from "./glossary";

type Props = {
  term: GlossaryKey;
  // 'block' renders a standalone (?) badge that takes its own line of
  // baseline. 'inline' (default) renders right next to the surrounding
  // text without breaking baseline alignment.
  inline?: boolean;
  // Override placement. 'auto' (default) puts the tooltip below the
  // trigger; 'above' is useful when the trigger sits at the bottom of
  // a container and we'd otherwise overflow.
  placement?: "auto" | "above";
};

export default function HelpTip({ term, inline = true, placement = "auto" }: Props) {
  const entry = GLOSSARY[term];
  if (!entry) return null;

  const triggerClass = inline
    ? "relative inline-flex items-center align-middle"
    : "relative inline-flex items-center";

  // Tooltip sits in a 280px box. Anchored to the trigger; below by
  // default, above when placement === 'above'. Group-hover + focus-
  // within make it visible without JS.
  const tooltipPositionClass =
    placement === "above" ? "bottom-full mb-2" : "top-full mt-2";

  return (
    <span className={`group ${triggerClass}`}>
      <span
        role="button"
        tabIndex={0}
        aria-label={`What is ${entry.term}?`}
        className="inline-flex items-center justify-center w-4 h-4 rounded-full bg-gray-200 text-gray-500 hover:bg-gray-300 hover:text-gray-700 focus:outline-none focus:ring-2 focus:ring-accent-400 cursor-help text-[10px] font-semibold ml-1"
      >
        ?
      </span>
      <span
        role="tooltip"
        className={
          "pointer-events-none invisible opacity-0 group-hover:visible group-hover:opacity-100 group-focus-within:visible group-focus-within:opacity-100 absolute left-0 z-50 w-72 rounded-md border border-gray-200 bg-white shadow-lg p-3 text-xs text-gray-700 transition-opacity duration-100 " +
          tooltipPositionClass
        }
      >
        <span className="block font-semibold text-gray-900 mb-1">
          {entry.term}
        </span>
        <span className="block">{entry.description}</span>
        {entry.docsHref && (
          <Link
            href={entry.docsHref}
            className="pointer-events-auto mt-2 inline-block text-accent-600 hover:text-accent-700"
          >
            Read more →
          </Link>
        )}
      </span>
    </span>
  );
}
