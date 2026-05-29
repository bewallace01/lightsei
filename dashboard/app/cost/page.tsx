"use client";

import Link from "next/link";
import CostPanel from "../CostPanel";

export default function CostPage() {
  return (
    <main className="px-4 py-6 sm:px-8 sm:py-10 max-w-4xl mx-auto">
      <div className="flex items-baseline justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Cost</h1>
          <p className="text-sm text-gray-500 mt-1">
            Month-to-date spend, projected end-of-month, and the per-agent +
            per-model breakdown.
          </p>
        </div>
        <Link
          href="/cost/insights"
          className="px-4 py-2 bg-accent-600 text-white rounded-md text-sm font-medium hover:bg-accent-700 no-underline whitespace-nowrap"
        >
          ✨ insights
        </Link>
      </div>
      <CostPanel />
    </main>
  );
}
