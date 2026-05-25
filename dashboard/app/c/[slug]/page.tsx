"use client";

// Phase 26.2: per-vendor chat surface for end users.
//
// Two-pane layout on desktop (conversation list left, thread + composer
// right), drill-in on mobile. Conversations come from
// /me/end-user/vendors/{slug}/conversations; thread polling + sends
// go through the existing Phase 25.4 widget endpoints keyed off
// widget_public_id (so the orchestrator path is shared with the
// iframe widget).
//
// Polling cadence: 3s while a thread is open. Per Phase 26 spec
// SSE/WebSocket is parked until Phase 28.

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import {
  EndUserUnauthorizedError,
  EndUserVendor,
  EndUserVendorConversation,
  WidgetMessage,
  fetchEndUserVendorConversations,
  fetchWidgetThreadAsEndUser,
  postWidgetMessageAsEndUser,
} from "../../api";

const POLL_INTERVAL_MS = 3000;

type Setup =
  | { kind: "loading" }
  | {
      kind: "ok";
      vendor: EndUserVendor;
      conversations: EndUserVendorConversation[];
    }
  | { kind: "needs-signin" }
  | { kind: "not-found" }
  | { kind: "error"; message: string };

export default function VendorChatPage() {
  const params = useParams();
  const router = useRouter();
  const slug = String(params?.slug ?? "");
  const [setup, setSetup] = useState<Setup>({ kind: "loading" });
  const [activeConvId, setActiveConvId] = useState<string | null>(null);

  const loadList = useCallback(async () => {
    try {
      const data = await fetchEndUserVendorConversations(slug);
      setSetup({
        kind: "ok",
        vendor: data.vendor,
        conversations: data.conversations,
      });
      // Auto-select the most-recent conversation on first load so
      // the right pane isn't empty.
      setActiveConvId((cur) => cur ?? data.conversations[0]?.id ?? null);
    } catch (e) {
      if (e instanceof EndUserUnauthorizedError) {
        setSetup({ kind: "needs-signin" });
        return;
      }
      const msg = (e as Error).message || "";
      if (msg.toLowerCase().includes("not linked")) {
        setSetup({ kind: "not-found" });
        return;
      }
      setSetup({ kind: "error", message: msg });
    }
  }, [slug]);

  useEffect(() => {
    loadList();
  }, [loadList]);

  if (setup.kind === "loading") {
    return (
      <main className="min-h-screen flex items-center justify-center text-sm text-gray-400">
        loading…
      </main>
    );
  }

  if (setup.kind === "needs-signin") {
    return (
      <main className="min-h-screen px-6 py-16 max-w-md mx-auto text-center">
        <h1 className="text-2xl font-semibold tracking-tight mb-3">
          Sign in to continue
        </h1>
        <p className="text-sm text-gray-500">
          Your session expired. Ask the vendor to send you a fresh
          magic-link email.
        </p>
      </main>
    );
  }

  if (setup.kind === "not-found") {
    return (
      <main className="min-h-screen px-6 py-16 max-w-md mx-auto text-center">
        <h1 className="text-2xl font-semibold tracking-tight mb-3">
          Vendor not found
        </h1>
        <p className="text-sm text-gray-500 mb-6">
          Either this vendor doesn&apos;t exist, or you haven&apos;t
          been invited yet.
        </p>
        <Link href="/c" className="text-sm text-indigo-600 hover:text-indigo-700">
          ← Back to your chats
        </Link>
      </main>
    );
  }

  if (setup.kind === "error") {
    return (
      <main className="min-h-screen px-6 py-16 max-w-md mx-auto text-center">
        <p className="text-sm text-red-600">{setup.message}</p>
        <Link
          href="/c"
          className="block mt-6 text-sm text-indigo-600 hover:text-indigo-700"
        >
          ← Back to your chats
        </Link>
      </main>
    );
  }

  const { vendor, conversations } = setup;

  return (
    <main className="min-h-screen flex flex-col">
      <header className="border-b border-gray-200 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link href="/c" className="text-sm text-gray-500 hover:text-gray-800">
            ←
          </Link>
          <div>
            <div className="text-base font-medium text-gray-900">
              {vendor.name}
            </div>
            {vendor.customer_facing_agent_name && (
              <div className="text-xs text-gray-500">
                Chatting with {vendor.customer_facing_agent_name}
              </div>
            )}
          </div>
        </div>
      </header>

      <div className="flex flex-1 min-h-0">
        <aside className="w-64 border-r border-gray-200 overflow-y-auto hidden md:block">
          <ConversationList
            conversations={conversations}
            activeId={activeConvId}
            onPick={setActiveConvId}
            onNew={() => setActiveConvId(null)}
          />
        </aside>

        <section className="flex-1 flex flex-col min-h-0">
          {vendor.widget_public_id ? (
            <ChatPane
              publicId={vendor.widget_public_id}
              conversationId={activeConvId}
              onConversationCreated={(newId) => {
                setActiveConvId(newId);
                // Refresh the list so the new conv shows up at the top.
                loadList();
              }}
            />
          ) : (
            <div className="flex-1 flex items-center justify-center text-sm text-gray-500 px-6 text-center">
              This vendor hasn&apos;t finished setting up their chat
              widget yet. Try again later.
            </div>
          )}
        </section>
      </div>
    </main>
  );
}

function ConversationList({
  conversations,
  activeId,
  onPick,
  onNew,
}: {
  conversations: EndUserVendorConversation[];
  activeId: string | null;
  onPick: (id: string) => void;
  onNew: () => void;
}) {
  return (
    <div className="p-3">
      <button
        type="button"
        onClick={onNew}
        className="w-full mb-3 text-sm rounded-md bg-indigo-600 text-white py-2 hover:bg-indigo-500"
      >
        + New conversation
      </button>
      {conversations.length === 0 ? (
        <div className="text-xs text-gray-400 px-2 py-3">
          No conversations yet. Start one above.
        </div>
      ) : (
        <ul className="space-y-1">
          {conversations.map((c) => (
            <li key={c.id}>
              <button
                type="button"
                onClick={() => onPick(c.id)}
                className={
                  "w-full text-left rounded-md px-3 py-2 text-sm transition-colors " +
                  (activeId === c.id
                    ? "bg-indigo-50 text-indigo-900"
                    : "hover:bg-gray-50 text-gray-700")
                }
              >
                <div className="font-medium truncate">
                  {c.customer_facing_agent_name ?? "Conversation"}
                </div>
                <div className="text-[11px] text-gray-500 mt-0.5">
                  {new Date(c.last_message_at).toLocaleString()}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function ChatPane({
  publicId,
  conversationId,
  onConversationCreated,
}: {
  publicId: string;
  conversationId: string | null;
  onConversationCreated: (id: string) => void;
}) {
  const [messages, setMessages] = useState<WidgetMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Clear thread state when we switch conversations or kick off a new one.
  useEffect(() => {
    setMessages([]);
    setError(null);
  }, [conversationId]);

  // Poll the thread while a conversation is open.
  useEffect(() => {
    if (!conversationId) return;
    let alive = true;
    let highestSeen = 0;
    const tick = async () => {
      try {
        const thread = await fetchWidgetThreadAsEndUser(
          publicId, conversationId, highestSeen,
        );
        if (!alive) return;
        if (thread.messages.length > 0) {
          setMessages((cur) => [...cur, ...thread.messages]);
          highestSeen = Math.max(
            highestSeen,
            ...thread.messages.map((m) => m.id),
          );
        }
      } catch {
        // Swallow poll errors; the next tick will retry. A persistent
        // failure surfaces when the user tries to send.
      }
    };
    tick();
    const id = setInterval(tick, POLL_INTERVAL_MS);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [publicId, conversationId]);

  // Auto-scroll to the bottom when new messages land.
  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: "smooth",
    });
  }, [messages]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const text = draft.trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    try {
      const res = await postWidgetMessageAsEndUser(publicId, {
        text,
        ...(conversationId ? { conversation_id: conversationId } : {}),
      });
      setDraft("");
      // Optimistic-append the user's own message so it shows up
      // before the next poll tick.
      setMessages((cur) => [
        ...cur,
        {
          id: res.message_id,
          role: "user",
          text,
          sent_at: new Date().toISOString(),
        },
      ]);
      if (!conversationId) {
        onConversationCreated(res.conversation_id);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSending(false);
    }
  }

  return (
    <>
      <div
        ref={scrollRef}
        className="flex-1 overflow-y-auto px-6 py-4 space-y-3 bg-gray-50"
      >
        {messages.length === 0 ? (
          <div className="text-center text-sm text-gray-400 mt-12">
            {conversationId
              ? "loading messages…"
              : "Say hi to kick things off."}
          </div>
        ) : (
          messages.map((m) => <MessageBubble key={m.id} message={m} />)
        )}
      </div>
      {error && (
        <div className="px-6 py-2 text-xs text-red-600 bg-red-50 border-t border-red-200">
          {error}
        </div>
      )}
      <form
        onSubmit={onSubmit}
        className="border-t border-gray-200 px-4 py-3 flex gap-2 bg-white"
      >
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Type a message…"
          className="flex-1 text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-600"
          disabled={sending}
        />
        <button
          type="submit"
          disabled={sending || !draft.trim()}
          className="text-sm rounded-md bg-indigo-600 text-white px-4 py-2 hover:bg-indigo-500 disabled:opacity-50"
        >
          {sending ? "Sending…" : "Send"}
        </button>
      </form>
    </>
  );
}

function MessageBubble({ message }: { message: WidgetMessage }) {
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  if (isSystem) {
    return (
      <div className="text-center text-[11px] uppercase tracking-wider text-gray-400 py-2">
        {message.text}
      </div>
    );
  }
  return (
    <div className={"flex " + (isUser ? "justify-end" : "justify-start")}>
      <div
        className={
          "max-w-[75%] rounded-2xl px-4 py-2 text-sm " +
          (isUser
            ? "bg-indigo-600 text-white"
            : "bg-white text-gray-900 ring-1 ring-gray-200")
        }
      >
        {message.text}
      </div>
    </div>
  );
}
