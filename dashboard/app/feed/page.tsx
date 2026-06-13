"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { FeedItem, fetchFeed, handleAuthError } from "../api";
import EmptyState from "../EmptyState";

const POLL_MS = 10000;

function relTime(iso: string): string {
  try {
    const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.round(hrs / 24)}d ago`;
  } catch {
    return iso;
  }
}

function dayKey(iso: string): string {
  try {
    const d = new Date(iso);
    const today = new Date();
    const y = new Date(today);
    y.setDate(today.getDate() - 1);
    if (d.toDateString() === today.toDateString()) return "Today";
    if (d.toDateString() === y.toDateString()) return "Yesterday";
    return d.toLocaleDateString(undefined, {
      month: "short",
      day: "numeric",
    });
  } catch {
    return "Earlier";
  }
}

/**
 * The proactive feed: a timeline of what the AI team did, newest first,
 * grouped by day. Alerts (negative reviews, urgent emails, errors) carry a
 * red dot so they stand out. Polls so new items appear without a refresh.
 */
export default function FeedPage() {
  const router = useRouter();
  const [items, setItems] = useState<FeedItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await fetchFeed(100);
        if (!alive) return;
        setItems(data);
        setError(null);
      } catch (e) {
        if (!alive) return;
        if (handleAuthError(e, router)) return;
        setError(String(e));
      }
    };
    tick();
    const id = setInterval(tick, POLL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [router]);

  // Group consecutive items by day for the timeline headers.
  const groups: { day: string; items: FeedItem[] }[] = [];
  (items ?? []).forEach((it) => {
    const day = dayKey(it.timestamp);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.items.push(it);
    else groups.push({ day, items: [it] });
  });

  return (
    <main className="px-4 py-6 sm:px-8 sm:py-10 max-w-3xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-semibold tracking-tight">Team activity</h1>
        <p className="text-sm text-gray-500 mt-1">
          What your AI team has been doing, newest first.
        </p>
      </div>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {items === null ? (
        <div className="text-sm text-gray-400">loading…</div>
      ) : items.length === 0 ? (
        <EmptyState
          title="Nothing yet"
          body={
            <>
              Once your assistants run, what they do shows up here: weekly
              digests, flagged reviews, triaged emails, lead scores, and
              anything that needs your attention.
            </>
          }
          primary={{ href: "/welcome", label: "✨ Set up my team" }}
        />
      ) : (
        <div className="space-y-8">
          {groups.map((g) => (
            <div key={g.day}>
              <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider mb-3">
                {g.day}
              </div>
              <ul className="space-y-2">
                {g.items.map((it) => (
                  <li
                    key={it.id}
                    className="flex items-start gap-3 rounded-lg border border-gray-200 bg-white px-4 py-3"
                  >
                    <span
                      className={
                        "mt-1.5 h-2 w-2 shrink-0 rounded-full " +
                        (it.severity === "alert"
                          ? "bg-red-500"
                          : "bg-gray-300")
                      }
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="text-sm font-medium text-gray-900">
                          {it.title}
                        </span>
                        <span className="text-xs text-gray-400 whitespace-nowrap">
                          {relTime(it.timestamp)}
                        </span>
                      </div>
                      {it.detail && (
                        <div className="text-xs text-gray-500 mt-0.5 truncate">
                          {it.detail}
                        </div>
                      )}
                      <div className="text-[10px] uppercase tracking-wider text-gray-400 mt-1">
                        {it.assistant_label}
                      </div>
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      )}
    </main>
  );
}
