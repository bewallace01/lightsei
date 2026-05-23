"use client";

// Phase 21.8: operator inbox.
//
// Two-column layout. Left: conversation list with filter chips +
// 5s polling. Right: selected conversation thread + operator
// action bar (take-over, reply, resolve).
//
// Polling is naive (re-fetch the full list every 5s + re-fetch the
// selected thread on the same cadence); SSE parked to a follow-up
// per the Phase 21 spec. The shape supports a `since` cursor on
// the list endpoint, but the demo cadence works fine with a full
// refresh — operators rarely look at more than a few conversations
// at a time.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  InboxConversationDetail,
  InboxConversationRow,
  UnauthorizedError,
  fetchInbox,
  fetchInboxConversation,
  postInboxOperatorReply,
  resolveConversation,
  takeOverConversation,
} from "../api";
import { SensitivityChip } from "../sensitivity";


const POLL_INTERVAL_MS = 5000;


const STATUS_FILTERS: Array<{ key: string; label: string }> = [
  { key: "active", label: "Active" },
  { key: "escalated", label: "Escalated" },
  { key: "operator_owned", label: "I'm handling" },
  { key: "open", label: "Open" },
  { key: "resolved", label: "Resolved" },
  { key: "all", label: "All" },
];


type StatusBadge = {
  label: string;
  className: string;
};


function statusBadge(status: InboxConversationRow["status"]): StatusBadge {
  switch (status) {
    case "escalated":
      return {
        label: "Escalated",
        className:
          "bg-rose-900/40 border-rose-700/60 text-rose-200",
      };
    case "operator_owned":
      return {
        label: "Handling",
        className:
          "bg-amber-900/40 border-amber-700/60 text-amber-200",
      };
    case "resolved":
      return {
        label: "Resolved",
        className: "bg-zinc-800 border-zinc-700 text-zinc-400",
      };
    case "open":
    default:
      return {
        label: "Open",
        className:
          "bg-indigo-900/40 border-indigo-700/60 text-indigo-200",
      };
  }
}


function fmtRelative(iso: string): string {
  const then = new Date(iso).getTime();
  const now = Date.now();
  const diff = Math.max(0, now - then) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)}h ago`;
  return `${Math.round(diff / 86400)}d ago`;
}


export default function InboxPage(): JSX.Element {
  const router = useRouter();
  const [conversations, setConversations] = useState<InboxConversationRow[]>([]);
  const [filter, setFilter] = useState<string>("active");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<InboxConversationDetail | null>(null);
  const [loadingList, setLoadingList] = useState(true);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const [replyText, setReplyText] = useState("");
  const [busy, setBusy] = useState<"take" | "resolve" | "reply" | null>(null);

  const replyRef = useRef<HTMLTextAreaElement | null>(null);

  const handleAuthError = (e: unknown) => {
    if (e instanceof UnauthorizedError) {
      router.replace("/login");
      return true;
    }
    return false;
  };

  const refreshList = useCallback(
    async (preserveSelection: boolean = true) => {
      try {
        const resp = await fetchInbox({ status: filter });
        setConversations(resp.conversations);
        setError(null);
        // Auto-select the first conversation when nothing was
        // selected (most operators land on the inbox + want to
        // start triaging immediately).
        if (!preserveSelection || !selectedId) {
          const first = resp.conversations[0];
          if (first) setSelectedId(first.id);
        }
      } catch (e) {
        if (handleAuthError(e)) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoadingList(false);
      }
    },
    [filter, selectedId],
  );

  const refreshDetail = useCallback(
    async (conversationId: string | null) => {
      if (!conversationId) {
        setDetail(null);
        return;
      }
      try {
        setLoadingDetail(true);
        const d = await fetchInboxConversation(conversationId);
        setDetail(d);
      } catch (e) {
        if (handleAuthError(e)) return;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        setLoadingDetail(false);
      }
    },
    [],
  );

  // Initial + filter-change list refresh.
  useEffect(() => {
    setLoadingList(true);
    void refreshList(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filter]);

  // Detail refresh on selection change.
  useEffect(() => {
    void refreshDetail(selectedId);
  }, [selectedId, refreshDetail]);

  // Polling: re-fetch list + detail every POLL_INTERVAL_MS.
  useEffect(() => {
    const id = window.setInterval(() => {
      void refreshList(true);
      void refreshDetail(selectedId);
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(id);
  }, [refreshList, refreshDetail, selectedId]);

  const flashTimeout = (msg: string) => {
    setFlash(msg);
    setTimeout(() => setFlash(null), 2500);
  };

  const onTakeOver = async () => {
    if (!selectedId) return;
    setBusy("take");
    try {
      const resp = await takeOverConversation(selectedId);
      flashTimeout(resp.noop ? "Already in your queue." : "Taken over. Bot paused.");
      await Promise.all([refreshList(true), refreshDetail(selectedId)]);
      if (replyRef.current) replyRef.current.focus();
    } catch (e) {
      if (handleAuthError(e)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const onResolve = async () => {
    if (!selectedId) return;
    if (!confirm("Mark this conversation resolved?")) return;
    setBusy("resolve");
    try {
      await resolveConversation(selectedId);
      flashTimeout("Resolved.");
      await Promise.all([refreshList(true), refreshDetail(selectedId)]);
    } catch (e) {
      if (handleAuthError(e)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const onReply = async () => {
    if (!selectedId || !replyText.trim() || busy === "reply") return;
    setBusy("reply");
    try {
      await postInboxOperatorReply(selectedId, replyText.trim());
      setReplyText("");
      await Promise.all([refreshList(true), refreshDetail(selectedId)]);
    } catch (e) {
      if (handleAuthError(e)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const onReplyKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      void onReply();
    }
  };

  const escalationOpen = useMemo(
    () =>
      detail?.escalations.find((e) => e.resolved_at === null) || null,
    [detail],
  );

  return (
    <main className="max-w-6xl mx-auto px-6 py-6 text-sm text-zinc-200">
      <header className="mb-4 flex items-baseline justify-between">
        <div>
          <h1 className="text-xl font-semibold">Inbox</h1>
          <p className="text-zinc-400 mt-1 text-xs">
            Conversations from your customer-facing widget. Escalated
            and operator-owned threads bubble to the top.
          </p>
        </div>
        <Link
          href="/widget-settings"
          className="text-xs text-zinc-400 hover:text-zinc-200 underline"
        >
          Widget settings →
        </Link>
      </header>

      {flash && (
        <div className="mb-3 rounded border border-emerald-700/60 bg-emerald-900/30 px-3 py-1.5 text-emerald-200 text-xs">
          {flash}
        </div>
      )}
      {error && (
        <div className="mb-3 rounded border border-rose-700/60 bg-rose-900/30 px-3 py-2 text-rose-200">
          {error}
        </div>
      )}

      {/* Filter chips */}
      <div className="mb-3 flex flex-wrap gap-1">
        {STATUS_FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            onClick={() => setFilter(f.key)}
            className={
              "rounded-full px-3 py-0.5 text-xs border " +
              (filter === f.key
                ? "bg-indigo-600/30 border-indigo-600 text-indigo-200"
                : "border-zinc-700 text-zinc-400 hover:bg-zinc-800")
            }
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[320px,1fr] gap-3">
        {/* Conversation list */}
        <aside className="rounded-lg border border-zinc-800 bg-zinc-950/60 overflow-hidden">
          {loadingList && conversations.length === 0 ? (
            <p className="p-3 text-zinc-500">Loading…</p>
          ) : conversations.length === 0 ? (
            <p className="p-3 text-zinc-500 italic">
              No conversations match this filter.
            </p>
          ) : (
            <ul className="divide-y divide-zinc-800 max-h-[calc(100vh-220px)] overflow-y-auto">
              {conversations.map((c) => {
                const badge = statusBadge(c.status);
                const selected = c.id === selectedId;
                return (
                  <li key={c.id}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(c.id)}
                      className={
                        "w-full text-left px-3 py-2 hover:bg-zinc-900 " +
                        (selected ? "bg-zinc-900" : "")
                      }
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span
                          className={
                            "text-[10px] uppercase tracking-wide rounded-full border px-1.5 " +
                            badge.className
                          }
                        >
                          {badge.label}
                        </span>
                        {c.sensitivity_level && (
                          <SensitivityChip level={c.sensitivity_level} size="sm" />
                        )}
                      </div>
                      <p className="text-xs text-zinc-300 mt-1 line-clamp-2">
                        {c.last_message_preview || (
                          <span className="text-zinc-600 italic">
                            (no messages yet)
                          </span>
                        )}
                      </p>
                      <div className="flex items-center justify-between mt-1 text-[10px] text-zinc-500">
                        <span>{c.customer_facing_agent_name || "—"}</span>
                        <span>{fmtRelative(c.last_message_at)}</span>
                      </div>
                      {c.open_escalation_count > 0 && (
                        <div className="text-[10px] text-rose-300 mt-0.5">
                          {c.open_escalation_count} open escalation
                          {c.open_escalation_count > 1 ? "s" : ""}
                        </div>
                      )}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </aside>

        {/* Thread view */}
        <section className="rounded-lg border border-zinc-800 bg-zinc-950/60 flex flex-col min-h-[calc(100vh-220px)]">
          {!selectedId ? (
            <div className="p-6 text-zinc-500 text-xs italic">
              Pick a conversation from the list to view it.
            </div>
          ) : !detail ? (
            <div className="p-6 text-zinc-500 text-xs italic">
              {loadingDetail ? "Loading…" : "No detail loaded."}
            </div>
          ) : (
            <>
              {/* Thread header */}
              <header className="px-4 py-2.5 border-b border-zinc-800 flex items-center justify-between">
                <div>
                  <div className="text-xs text-zinc-500">
                    {detail.anon_user_id || "anonymous"}
                  </div>
                  <div className="text-sm font-semibold text-zinc-200">
                    {detail.customer_facing_agent_name || "—"}
                    {detail.sensitivity_level && (
                      <span className="ml-2">
                        <SensitivityChip
                          level={detail.sensitivity_level}
                          size="sm"
                        />
                      </span>
                    )}
                  </div>
                </div>
                <span
                  className={
                    "text-[10px] uppercase tracking-wide rounded-full border px-1.5 " +
                    statusBadge(detail.status).className
                  }
                >
                  {statusBadge(detail.status).label}
                </span>
              </header>

              {/* Escalation panel (when there's an open one) */}
              {escalationOpen && (
                <div className="px-4 py-2 border-b border-zinc-800 bg-rose-950/20">
                  <div className="text-xs font-medium text-rose-200">
                    Open escalation: {escalationOpen.reason}
                  </div>
                  {Object.keys(escalationOpen.payload || {}).length > 0 && (
                    <pre className="text-[11px] text-rose-300/80 mt-1 whitespace-pre-wrap">
                      {JSON.stringify(escalationOpen.payload, null, 2)}
                    </pre>
                  )}
                  {/* 21.9 will add an "Apply suggested fix" button here
                      when escalationOpen.suggested_fix is non-null. */}
                </div>
              )}

              {/* Messages pane */}
              <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
                {detail.messages.length === 0 ? (
                  <p className="text-xs text-zinc-500 italic">
                    No messages yet.
                  </p>
                ) : (
                  detail.messages.map((m) => (
                    <InboxBubble key={m.id} message={m} />
                  ))
                )}
              </div>

              {/* Action bar */}
              {detail.status === "resolved" ? (
                <div className="px-4 py-3 border-t border-zinc-800 text-xs text-zinc-500 italic">
                  This conversation has been resolved. Reopening is not
                  supported in v1.
                </div>
              ) : (
                <div className="px-4 py-2 border-t border-zinc-800 flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    {detail.status !== "operator_owned" && (
                      <button
                        type="button"
                        onClick={() => void onTakeOver()}
                        disabled={busy === "take"}
                        className="rounded bg-amber-700 hover:bg-amber-600 px-3 py-1 text-xs text-white disabled:opacity-50"
                      >
                        {busy === "take" ? "…" : "Take over"}
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => void onResolve()}
                      disabled={busy === "resolve"}
                      className="rounded border border-emerald-700/60 text-emerald-300 px-3 py-1 text-xs hover:bg-emerald-900/30 disabled:opacity-50"
                    >
                      {busy === "resolve" ? "…" : "Mark resolved"}
                    </button>
                    <span className="text-[11px] text-zinc-500 ml-auto">
                      ⌘+Enter to send
                    </span>
                  </div>
                  <div className="flex items-end gap-2">
                    <textarea
                      ref={replyRef}
                      value={replyText}
                      onChange={(e) => setReplyText(e.target.value)}
                      onKeyDown={onReplyKeyDown}
                      placeholder={
                        detail.status === "operator_owned"
                          ? "Type a reply as the operator…"
                          : "Reply to chime in (use Take over to pause the bot)…"
                      }
                      rows={2}
                      className="flex-1 resize-none rounded border border-zinc-700 bg-black/40 px-2 py-1.5 text-xs text-zinc-100 focus:outline-none focus:ring-1 focus:ring-indigo-400"
                    />
                    <button
                      type="button"
                      onClick={() => void onReply()}
                      disabled={!replyText.trim() || busy === "reply"}
                      className="rounded bg-indigo-600 hover:bg-indigo-500 px-3 py-1.5 text-xs text-white disabled:opacity-50"
                    >
                      {busy === "reply" ? "…" : "Send"}
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </main>
  );
}


function InboxBubble({
  message,
}: {
  message: { role: string; text: string };
}): JSX.Element {
  const role = message.role;
  const alignRight = role === "operator";  // operator = "us" in the inbox view
  let bubble = "bg-zinc-800 text-zinc-200";  // default
  let prefix = "";
  if (role === "user") bubble = "bg-zinc-900 border border-zinc-700 text-zinc-200";
  else if (role === "bot") bubble = "bg-indigo-900/30 border border-indigo-700/40 text-indigo-100";
  else if (role === "operator") bubble = "bg-emerald-900/40 border border-emerald-700/60 text-emerald-100";
  else if (role === "system") {
    bubble = "bg-amber-900/20 border border-amber-700/40 text-amber-200/80 italic text-xs";
    prefix = "system · ";
  }
  return (
    <div className={`flex ${alignRight ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] rounded-lg px-3 py-1.5 text-sm whitespace-pre-wrap ${bubble}`}
      >
        <span className="text-[10px] uppercase tracking-wide opacity-60 mr-1">
          {prefix}{role !== "system" ? role : ""}
        </span>
        <br />
        {message.text}
      </div>
    </div>
  );
}
