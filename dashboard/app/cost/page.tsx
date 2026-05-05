"use client";

import CostPanel from "../CostPanel";

export default function CostPage() {
  return (
    <main className="px-8 py-10 max-w-4xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">Cost</h1>
        <p className="text-sm text-gray-500 mt-1">
          Month-to-date spend, projected end-of-month, and the per-agent +
          per-model breakdown that drives Phase 12&apos;s multi-provider
          story.
        </p>
      </div>
      <CostPanel />
    </main>
  );
}
