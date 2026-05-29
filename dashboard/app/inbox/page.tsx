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
  applyEscalationSuggestedFix,
  dismissEscalationSuggestedFix,
  fetchInbox,
  fetchInboxConversation,
  postInboxOperatorReply,
  resolveConversation,
  scanWidgetIncidentPatterns,
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
        className: "bg-red-100 border-red-200 text-red-800",
      };
    case "operator_owned":
      return {
        label: "Handling",
        className: "bg-amber-100 border-amber-200 text-amber-800",
      };
    case "resolved":
      return {
        label: "Resolved",
        className: "bg-gray-100 border-gray-200 text-gray-600",
      };
    case "open":
    default:
      return {
        label: "Open",
        className: "bg-indigo-100 border-indigo-200 text-indigo-800",
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
  const [busy, setBusy] = useState<
    "take" | "resolve" | "reply" | "apply" | "dismiss" | "scan" | null
  >(null);

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

  const onScanPatterns = async () => {
    setBusy("scan");
    try {
      const r = await scanWidgetIncidentPatterns();
      if (r.fixes_generated === 0) {
        flashTimeout(
          r.clusters_found === 0
            ? "No escalation patterns detected."
            : `Found ${r.clusters_found} cluster${r.clusters_found > 1 ? "s" : ""}, but no fixes generated.`,
        );
      } else {
        const appliedSuffix = r.fixes_applied
          ? ` ${r.fixes_applied} auto-applied.`
          : "";
        flashTimeout(
          `${r.fixes_generated} suggested fix${r.fixes_generated > 1 ? "es" : ""} ready.${appliedSuffix}`,
        );
      }
      await Promise.all([refreshList(true), refreshDetail(selectedId)]);
    } catch (e) {
      if (handleAuthError(e)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const onApplyFix = async (escalationId: string) => {
    if (!selectedId) return;
    setBusy("apply");
    try {
      await applyEscalationSuggestedFix(selectedId, escalationId);
      flashTimeout("Fix applied. Bot's system prompt updated.");
      await Promise.all([refreshList(true), refreshDetail(selectedId)]);
    } catch (e) {
      if (handleAuthError(e)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const onDismissFix = async (escalationId: string) => {
    if (!selectedId) return;
    setBusy("dismiss");
    try {
      await dismissEscalationSuggestedFix(selectedId, escalationId);
      flashTimeout("Suggestion dismissed.");
      await Promise.all([refreshList(true), refreshDetail(selectedId)]);
    } catch (e) {
      if (handleAuthError(e)) return;
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const escalationOpen = useMemo(
    () =>
      detail?.escalations.find((e) => e.resolved_at === null) || null,
    [detail],
  );

  return (
    <main className="max-w-6xl mx-auto px-4 py-5 sm:px-6 sm:py-6 text-sm text-gray-900">
      <header className="mb-4 flex items-baseline justify-between gap-2">
        <div>
          <h1 className="text-xl font-semibold">Inbox</h1>
          <p className="text-gray-600 mt-1 text-xs">
            Conversations from your customer-facing widget. Escalated
            and operator-owned threads bubble to the top.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => void onScanPatterns()}
            disabled={busy === "scan"}
            className="rounded border border-gray-200 px-3 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            title="Scan open escalations for patterns and generate suggested fixes"
          >
            {busy === "scan" ? "Scanning…" : "Scan for patterns"}
          </button>
          <Link
            href="/widget-settings"
            className="text-xs text-gray-500 hover:text-gray-900 underline"
          >
            Widget settings →
          </Link>
        </div>
      </header>

      {flash && (
        <div className="mb-3 rounded border border-green-200 bg-green-50 px-3 py-1.5 text-green-800 text-xs">
          {flash}
        </div>
      )}
      {error && (
        <div className="mb-3 rounded border border-red-200 bg-red-50 px-3 py-2 text-red-700">
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
                ? "bg-accent-600 border-accent-600 text-white"
                : "border-gray-200 text-gray-600 hover:bg-gray-50")
            }
          >
            {f.label}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-[320px,1fr] gap-3">
        {/* Conversation list */}
        <aside className="rounded-lg border border-gray-200 bg-white overflow-hidden">
          {loadingList && conversations.length === 0 ? (
            <p className="p-3 text-gray-500">Loading…</p>
          ) : conversations.length === 0 ? (
            <p className="p-3 text-gray-500 italic">
              No conversations match this filter.
            </p>
          ) : (
            <ul className="divide-y divide-gray-100 md:max-h-[calc(100vh-220px)] md:overflow-y-auto">
              {conversations.map((c) => {
                const badge = statusBadge(c.status);
                const selected = c.id === selectedId;
                return (
                  <li key={c.id}>
                    <button
                      type="button"
                      onClick={() => setSelectedId(c.id)}
                      className={
                        "w-full text-left px-3 py-2 hover:bg-gray-50 " +
                        (selected ? "bg-gray-100" : "")
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
                      <p className="text-xs text-gray-700 mt-1 line-clamp-2">
                        {c.last_message_preview || (
                          <span className="text-gray-400 italic">
                            (no messages yet)
                          </span>
                        )}
                      </p>
                      <div className="flex items-center justify-between mt-1 text-[10px] text-gray-500">
                        <span>{c.customer_facing_agent_name || "—"}</span>
                        <span>{fmtRelative(c.last_message_at)}</span>
                      </div>
                      {c.open_escalation_count > 0 && (
                        <div className="text-[10px] text-red-700 mt-0.5">
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
        <section className="rounded-lg border border-gray-200 bg-white flex flex-col md:min-h-[calc(100vh-220px)]">
          {!selectedId ? (
            <div className="p-6 text-gray-500 text-xs italic">
              Pick a conversation from the list to view it.
            </div>
          ) : !detail ? (
            <div className="p-6 text-gray-500 text-xs italic">
              {loadingDetail ? "Loading…" : "No detail loaded."}
            </div>
          ) : (
            <>
              {/* Thread header */}
              <header className="px-4 py-2.5 border-b border-gray-200 flex items-center justify-between">
                <div>
                  <div className="text-xs text-gray-500">
                    {detail.anon_user_id || "anonymous"}
                  </div>
                  <div className="text-sm font-semibold text-gray-900">
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
                <div className="px-4 py-2 border-b border-gray-200 bg-red-50">
                  <div className="text-xs font-medium text-red-800">
                    Open escalation: {escalationOpen.reason}
                  </div>
                  {Object.keys(escalationOpen.payload || {}).length > 0 && (
                    <pre className="text-[11px] text-red-700 mt-1 whitespace-pre-wrap font-mono">
                      {JSON.stringify(escalationOpen.payload, null, 2)}
                    </pre>
                  )}
                  {escalationOpen.suggested_fix && (
                    <div className="mt-2 rounded border border-indigo-200 bg-indigo-50 p-2">
                      <div className="text-xs font-medium text-indigo-900 mb-1">
                        Polaris suggested:{" "}
                        <span className="font-normal text-indigo-700">
                          {String(
                            escalationOpen.suggested_fix.summary ||
                              escalationOpen.suggested_fix.kind ||
                              "an improvement",
                          )}
                        </span>
                      </div>
                      {typeof escalationOpen.suggested_fix.detail === "string" && (
                        <pre className="text-[11px] text-indigo-900 whitespace-pre-wrap mb-2 font-mono">
                          {String(escalationOpen.suggested_fix.detail)}
                        </pre>
                      )}
                      <div className="flex items-center gap-2">
                        <button
                          type="button"
                          onClick={() => void onApplyFix(escalationOpen.id)}
                          disabled={busy === "apply"}
                          className="rounded bg-accent-600 hover:bg-accent-700 px-2.5 py-1 text-xs text-white disabled:opacity-50"
                        >
                          {busy === "apply" ? "Applying…" : "Apply suggested fix"}
                        </button>
                        <button
                          type="button"
                          onClick={() => void onDismissFix(escalationOpen.id)}
                          disabled={busy === "dismiss"}
                          className="rounded border border-gray-200 px-2.5 py-1 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50"
                        >
                          {busy === "dismiss" ? "…" : "Dismiss"}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Messages pane */}
              <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
                {detail.messages.length === 0 ? (
                  <p className="text-xs text-gray-500 italic">
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
                <div className="px-4 py-3 border-t border-gray-200 text-xs text-gray-500 italic">
                  This conversation has been resolved. Reopening is not
                  supported in v1.
                </div>
              ) : (
                <div className="px-4 py-2 border-t border-gray-200 flex flex-col gap-2">
                  <div className="flex items-center gap-2">
                    {detail.status !== "operator_owned" && (
                      <button
                        type="button"
                        onClick={() => void onTakeOver()}
                        disabled={busy === "take"}
                        className="rounded bg-amber-500 hover:bg-amber-600 px-3 py-1 text-xs text-white disabled:opacity-50"
                      >
                        {busy === "take" ? "…" : "Take over"}
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => void onResolve()}
                      disabled={busy === "resolve"}
                      className="rounded border border-emerald-200 text-emerald-700 px-3 py-1 text-xs hover:bg-emerald-50 disabled:opacity-50"
                    >
                      {busy === "resolve" ? "…" : "Mark resolved"}
                    </button>
                    <span className="text-[11px] text-gray-500 ml-auto">
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
                      className="flex-1 resize-none rounded border border-gray-300 bg-white px-2 py-1.5 text-xs text-gray-900 focus:outline-none focus:ring-1 focus:ring-accent-500"
                    />
                    <button
                      type="button"
                      onClick={() => void onReply()}
                      disabled={!replyText.trim() || busy === "reply"}
                      className="rounded bg-accent-600 hover:bg-accent-700 px-3 py-1.5 text-xs text-white disabled:opacity-50"
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
  let bubble = "bg-gray-100 text-gray-900";  // default
  let prefix = "";
  if (role === "user") bubble = "bg-gray-50 border border-gray-200 text-gray-900";
  else if (role === "bot") bubble = "bg-indigo-50 border border-indigo-200 text-indigo-900";
  else if (role === "operator") bubble = "bg-emerald-50 border border-emerald-200 text-emerald-900";
  else if (role === "system") {
    bubble = "bg-amber-50 border border-amber-200 text-amber-800 italic text-xs";
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
