"use client";

// Phase 21.3: widget iframe app.
//
// Served at app.lightsei.com/widget/{publicId}. The customer-side
// snippet (Phase 21.4) injects an iframe pointing at this URL into
// any page on the customer's site. The end user never sees a
// Lightsei dashboard chrome — Header.tsx bails on /widget/* paths.
//
// Surface (Phase 21.3 scope):
//
//   - Header: bot display name + small "About" link that opens a
//     trust-zone disclosure modal.
//   - Conversation pane: chronological message list, role-tinted.
//   - Input: textarea + Send button; Enter sends, Shift+Enter
//     newlines.
//   - Footer: "Anonymous conversation. Powered by Lightsei."
//   - postMessage to parent for iframe-height changes so the
//     customer-side script (21.4) can resize without scrollbars.
//
// State:
//
//   - localStorage `lightsei.widget.conv.{publicId}` carries the
//     conversation_id across page reloads on the same customer
//     site. Cleared when the user clicks "Start over".
//   - Anonymous user id (`lightsei.widget.anon.{publicId}`) is a
//     random local-only string so the workspace's inbox can group
//     anonymous conversations from the same end user.
//
// Backend contract: hits the three Phase 21.2 public endpoints
// (config / messages / conversations/{id}). All unauthenticated;
// Origin allowlist on the backend side is the actual gate.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";

import { API_URL } from "../../api";


type Role = "user" | "bot" | "operator" | "system";

type WidgetMessage = {
  id: number;
  role: Role;
  text: string;
  sent_at: string;
};

type WidgetConfig = {
  public_id: string;
  bot:
    | {
        name: string;
        description: string | null;
        sensitivity_level: string;
      }
    | null;
  anonymous: boolean;
};

type ConversationResponse = {
  conversation_id: string;
  status: string;
  messages: WidgetMessage[];
};

const POLL_INTERVAL_MS = 1500;
const ANON_ID_LEN = 24;
const EMBED_ORIGIN_HEADER = "x-lightsei-embed-origin";


// ---------- Local storage helpers ---------- //

function convStorageKey(publicId: string): string {
  return `lightsei.widget.conv.${publicId}`;
}

function anonStorageKey(publicId: string): string {
  return `lightsei.widget.anon.${publicId}`;
}

function loadStored(publicId: string): {
  conversationId: string | null;
  anonUserId: string;
} {
  if (typeof window === "undefined") return { conversationId: null, anonUserId: "" };
  const conversationId = window.localStorage.getItem(convStorageKey(publicId));
  let anonUserId = window.localStorage.getItem(anonStorageKey(publicId));
  if (!anonUserId) {
    // 128 bits of randomness, base36-encoded so it stays readable in
    // logs. Local-only; the server never knows whether two
    // visitors share an anonymous id across origins.
    anonUserId = Array.from(crypto.getRandomValues(new Uint8Array(16)))
      .map((b) => b.toString(36).padStart(2, "0"))
      .join("")
      .slice(0, ANON_ID_LEN);
    window.localStorage.setItem(anonStorageKey(publicId), anonUserId);
  }
  return { conversationId, anonUserId };
}

function saveConversationId(publicId: string, conversationId: string | null): void {
  if (typeof window === "undefined") return;
  if (conversationId) {
    window.localStorage.setItem(convStorageKey(publicId), conversationId);
  } else {
    window.localStorage.removeItem(convStorageKey(publicId));
  }
}

function getEmbedOrigin(): string | null {
  if (typeof window === "undefined") return null;
  const ancestorOrigins = (window.location as Location & {
    ancestorOrigins?: DOMStringList;
  }).ancestorOrigins;
  if (ancestorOrigins && ancestorOrigins.length > 0) {
    return ancestorOrigins[0] || null;
  }
  if (document.referrer) {
    try {
      return new URL(document.referrer).origin;
    } catch {
      return null;
    }
  }
  return null;
}

function widgetHeaders(
  embedOrigin: string | null,
  extra?: Record<string, string>,
): Record<string, string> {
  const headers: Record<string, string> = { ...(extra || {}) };
  if (embedOrigin) headers[EMBED_ORIGIN_HEADER] = embedOrigin;
  return headers;
}


// ---------- Trust-zone copy ---------- //

const ZONE_DESCRIPTIONS: Record<string, string> = {
  public:
    "This bot is in the public trust zone. It cannot access customer accounts, private documents, or internal email.",
  internal:
    "This bot is in the internal trust zone. It may access workspace documents but cannot read customer-specific data.",
  sensitive:
    "This bot is in the sensitive trust zone. It may access workspace data including customer records.",
  pii:
    "This bot is in the PII trust zone. It may access personally identifiable customer information.",
};


// ---------- Component ---------- //

export default function WidgetIframePage(): JSX.Element {
  const params = useParams<{ publicId: string }>();
  const publicId = params?.publicId;

  const [config, setConfig] = useState<WidgetConfig | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [anonUserId, setAnonUserId] = useState<string>("");
  const [messages, setMessages] = useState<WidgetMessage[]>([]);
  const [conversationStatus, setConversationStatus] = useState<string | null>(null);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [postError, setPostError] = useState<string | null>(null);
  const [showAbout, setShowAbout] = useState(false);

  const seenMessageIdRef = useRef<number>(0);
  const paneRef = useRef<HTMLDivElement | null>(null);
  const embedOrigin = useMemo(() => getEmbedOrigin(), []);

  // ---------- Mount: load stored + fetch config ---------- //

  useEffect(() => {
    if (!publicId) return;
    const stored = loadStored(publicId);
    setConversationId(stored.conversationId);
    setAnonUserId(stored.anonUserId);

    let cancelled = false;
    (async () => {
      try {
        const r = await fetch(`${API_URL}/widget/${publicId}/config`, {
          credentials: "omit",
          headers: widgetHeaders(embedOrigin),
        });
        if (!r.ok) {
          const body = await r.json().catch(() => ({}));
          throw new Error(body?.detail?.message || `config returned ${r.status}`);
        }
        const data = (await r.json()) as WidgetConfig;
        if (!cancelled) setConfig(data);
      } catch (e) {
        if (!cancelled) setLoadError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [publicId, embedOrigin]);

  // ---------- Polling ---------- //

  const fetchUpdates = useCallback(async () => {
    if (!publicId || !conversationId) return;
    try {
      const r = await fetch(
        `${API_URL}/widget/${publicId}/conversations/${conversationId}?since=${seenMessageIdRef.current}`,
        {
          credentials: "omit",
          headers: widgetHeaders(embedOrigin),
        },
      );
      if (!r.ok) {
        if (r.status === 404) {
          // Conversation gone (operator deleted it or our id is
          // stale). Reset so the next message starts fresh.
          saveConversationId(publicId, null);
          setConversationId(null);
          setMessages([]);
          seenMessageIdRef.current = 0;
        }
        return;
      }
      const data = (await r.json()) as ConversationResponse;
      setConversationStatus(data.status);
      if (data.messages.length > 0) {
        setMessages((prev) => [...prev, ...data.messages]);
        const newest = data.messages[data.messages.length - 1];
        seenMessageIdRef.current = newest.id;
      }
    } catch {
      // Network blip — silently retry on the next tick.
    }
  }, [publicId, conversationId, embedOrigin]);

  // First load when the conversation id appears (mount path).
  useEffect(() => {
    if (conversationId) fetchUpdates();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  // Polling loop. Cancellable so the interval doesn't leak across
  // hot-reloads or props changes.
  useEffect(() => {
    if (!conversationId) return;
    const handle = window.setInterval(() => {
      void fetchUpdates();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(handle);
  }, [conversationId, fetchUpdates]);

  // Scroll-to-bottom when messages change.
  useEffect(() => {
    const pane = paneRef.current;
    if (pane) pane.scrollTop = pane.scrollHeight;
  }, [messages]);

  // ---------- postMessage parent (for iframe sizing in 21.4) ---------- //

  useEffect(() => {
    if (typeof window === "undefined" || window.parent === window) return;
    const ro = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const h = Math.ceil(entry.contentRect.height);
        window.parent.postMessage(
          { type: "lightsei:widget-resize", height: h },
          "*",
        );
      }
    });
    ro.observe(document.body);
    return () => ro.disconnect();
  }, []);

  // ---------- Send ---------- //

  const send = useCallback(async () => {
    if (!publicId || !input.trim() || sending) return;
    setSending(true);
    setPostError(null);
    const text = input.trim();
    // Optimistic render — the user's own message lands immediately
    // even before the POST resolves.
    const optimistic: WidgetMessage = {
      id: -Date.now(),
      role: "user",
      text,
      sent_at: new Date().toISOString(),
    };
    setMessages((prev) => [...prev, optimistic]);
    setInput("");

    try {
      const r = await fetch(`${API_URL}/widget/${publicId}/messages`, {
        method: "POST",
        credentials: "omit",
        headers: widgetHeaders(embedOrigin, { "content-type": "application/json" }),
        body: JSON.stringify({
          conversation_id: conversationId || null,
          text,
          anon_user_id: anonUserId || null,
        }),
      });
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        const message =
          body?.detail?.message ||
          (r.status === 429 ? "Slow down — too many messages." : `Send failed (${r.status})`);
        throw new Error(message);
      }
      const data = (await r.json()) as {
        conversation_id: string;
        message_id: number;
      };
      // Replace the optimistic id with the real one.
      setMessages((prev) =>
        prev.map((m) => (m.id === optimistic.id ? { ...m, id: data.message_id } : m)),
      );
      if (!conversationId) {
        setConversationId(data.conversation_id);
        saveConversationId(publicId, data.conversation_id);
      }
      seenMessageIdRef.current = Math.max(seenMessageIdRef.current, data.message_id);
    } catch (e) {
      // Roll back the optimistic message + surface the error.
      setMessages((prev) => prev.filter((m) => m.id !== optimistic.id));
      setInput(text);
      setPostError(e instanceof Error ? e.message : String(e));
    } finally {
      setSending(false);
    }
  }, [publicId, input, sending, conversationId, anonUserId, embedOrigin]);

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const startOver = () => {
    if (!publicId) return;
    saveConversationId(publicId, null);
    setConversationId(null);
    setMessages([]);
    seenMessageIdRef.current = 0;
    setConversationStatus(null);
  };

  // ---------- Render ---------- //

  const botName = config?.bot?.name ?? "Assistant";
  const botSensitivity = config?.bot?.sensitivity_level ?? null;

  const headerSubtitle = useMemo(() => {
    if (loadError) return "Connection error";
    if (!config) return "Loading…";
    if (!config.bot) return "Bot not configured";
    if (conversationStatus === "operator_owned")
      return "A human has joined this conversation";
    if (conversationStatus === "escalated") return "Handing off to a human…";
    return "Online";
  }, [loadError, config, conversationStatus]);

  return (
    <div className="min-h-screen bg-white text-gray-900 flex flex-col font-sans">
      <header className="border-b border-gray-200 px-4 py-3 flex items-center justify-between">
        <div>
          <div className="font-semibold text-sm">{botName}</div>
          <div className="text-xs text-gray-500">{headerSubtitle}</div>
        </div>
        <div className="flex items-center gap-2">
          {conversationId && (
            <button
              type="button"
              onClick={startOver}
              className="text-xs text-gray-500 hover:text-gray-800 underline"
            >
              Start over
            </button>
          )}
          <button
            type="button"
            onClick={() => setShowAbout(true)}
            className="text-xs text-gray-500 hover:text-gray-800 underline"
          >
            About this bot
          </button>
        </div>
      </header>

      <div ref={paneRef} className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
        {loadError && (
          <div className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            {loadError}
          </div>
        )}
        {!loadError && messages.length === 0 && (
          <div className="text-sm text-gray-500">
            Hi! Ask me anything about{" "}
            <span className="font-medium">{config?.bot?.description || "the product"}</span>.
          </div>
        )}
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        {postError && (
          <div className="rounded border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            {postError}
          </div>
        )}
      </div>

      <div className="border-t border-gray-200 px-3 py-2 flex items-end gap-2">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown}
          placeholder="Type a message…"
          rows={2}
          className="flex-1 resize-none rounded border border-gray-300 px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-400"
          disabled={!config || !!loadError || !config.bot}
        />
        <button
          type="button"
          onClick={() => void send()}
          disabled={!input.trim() || sending || !config || !!loadError || !config.bot}
          className="rounded bg-indigo-600 hover:bg-indigo-500 px-3 py-1.5 text-xs text-white disabled:opacity-50"
        >
          {sending ? "…" : "Send"}
        </button>
      </div>

      <footer className="border-t border-gray-100 px-3 py-1.5 text-[10px] text-gray-400 flex items-center justify-between">
        <span>Anonymous conversation.</span>
        <a
          href="https://lightsei.com"
          target="_blank"
          rel="noopener noreferrer"
          className="hover:text-gray-600"
        >
          Powered by Lightsei
        </a>
      </footer>

      {showAbout && (
        <AboutModal
          onClose={() => setShowAbout(false)}
          botName={botName}
          botDescription={config?.bot?.description ?? null}
          botSensitivity={botSensitivity}
        />
      )}
    </div>
  );
}


// ---------- Subcomponents ---------- //

function MessageBubble({ message }: { message: WidgetMessage }): JSX.Element {
  const alignRight = message.role === "user";
  const bubbleClass = (() => {
    if (message.role === "user") return "bg-indigo-600 text-white";
    if (message.role === "bot") return "bg-gray-100 text-gray-900";
    if (message.role === "operator")
      return "bg-emerald-50 border border-emerald-300 text-emerald-900";
    // system
    return "bg-amber-50 border border-amber-200 text-amber-800 text-xs italic";
  })();
  const prefix = (() => {
    if (message.role === "operator") return "Human · ";
    if (message.role === "system") return "";
    return "";
  })();

  return (
    <div
      className={`flex ${alignRight ? "justify-end" : "justify-start"}`}
    >
      <div
        className={`max-w-[85%] rounded-lg px-3 py-1.5 text-sm whitespace-pre-wrap ${bubbleClass}`}
      >
        {prefix}
        {message.text}
      </div>
    </div>
  );
}


function AboutModal({
  onClose,
  botName,
  botDescription,
  botSensitivity,
}: {
  onClose: () => void;
  botName: string;
  botDescription: string | null;
  botSensitivity: string | null;
}): JSX.Element {
  const zoneCopy = botSensitivity
    ? ZONE_DESCRIPTIONS[botSensitivity] || `This bot is in the ${botSensitivity} trust zone.`
    : null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/30 px-3"
      onClick={onClose}
    >
      <div
        className="max-w-md w-full bg-white rounded-lg shadow-xl border border-gray-200 p-5 text-sm"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="font-semibold text-base mb-1">About {botName}</h2>
        {botDescription && (
          <p className="text-gray-600 mb-3 text-xs">{botDescription}</p>
        )}
        <h3 className="text-xs font-semibold mt-3 mb-1 text-gray-700">
          Trust zone
        </h3>
        <p className="text-xs text-gray-600">
          {zoneCopy ||
            "Trust zone information isn't available for this bot."}
        </p>
        <h3 className="text-xs font-semibold mt-3 mb-1 text-gray-700">
          Conversation
        </h3>
        <p className="text-xs text-gray-600">
          This conversation is anonymous. The bot doesn't know who you
          are unless you tell it. A site operator may join the conversation
          to help if needed.
        </p>
        <div className="mt-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="rounded bg-gray-900 hover:bg-gray-700 px-3 py-1.5 text-xs text-white"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
