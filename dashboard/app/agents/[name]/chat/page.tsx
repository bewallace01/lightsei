"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  createThread,
  deleteThread,
  getThread,
  listThreads,
  postThreadMessage,
  Thread,
  ThreadMessage,
  UnauthorizedError,
} from "../../../api";
import Header from "../../../Header";

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function bubbleColors(role: string): string {
  switch (role) {
    case "user":
      return "bg-accent-50 border-accent-200 text-accent-900";
    case "assistant":
      return "bg-gray-50 border-gray-200 text-gray-900";
    case "system":
      return "bg-amber-50 border-amber-200 text-amber-900";
    default:
      return "bg-gray-50 border-gray-200 text-gray-700";
  }
}

export default function ChatPage({ params }: { params: { name: string } }) {
  const agentName = decodeURIComponent(params.name);
  const router = useRouter();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [activeThreadId, setActiveThreadId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ThreadMessage[]>([]);
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const messagesRef = useRef<HTMLDivElement | null>(null);

  // Load thread list
  const loadThreads = async (selectFirst = false) => {
    try {
      const t = await listThreads(agentName);
      setThreads(t);
      if (selectFirst && !activeThreadId && t.length > 0) {
        setActiveThreadId(t[0].id);
      }
      setError(null);
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(String(e));
    }
  };

  // Load active thread's messages
  const loadActive = async () => {
    if (!activeThreadId) return;
    try {
      const data = await getThread(activeThreadId);
      setMessages(data.messages);
    } catch (e) {
      if (e instanceof UnauthorizedError) {
        router.replace("/login");
        return;
      }
      setError(String(e));
    }
  };

  useEffect(() => {
    loadThreads(true);
    const id = setInterval(() => loadThreads(false), 5000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentName]);

  useEffect(() => {
    if (!activeThreadId) return;
    loadActive();
    const id = setInterval(loadActive, 1000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeThreadId]);

  // auto-scroll on new messages
  useEffect(() => {
    if (messagesRef.current) {
      messagesRef.current.scrollTop = messagesRef.current.scrollHeight;
    }
  }, [messages]);

  const onNewThread = async () => {
    try {
      const t = await createThread(agentName);
      await loadThreads();
      setActiveThreadId(t.id);
      setMessages([]);
    } catch (e) {
      setError(String(e));
    }
  };

  const onSend = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!draft.trim() || !activeThreadId) return;
    setSending(true);
    const text = draft.trim();
    setDraft("");
    try {
      await postThreadMessage(activeThreadId, text);
      await loadActive();
      await loadThreads(); // refresh title (auto-titled from first user msg)
    } catch (e) {
      setError(String(e));
      setDraft(text); // restore on error
    } finally {
      setSending(false);
    }
  };

  const onDeleteThread = async (id: string) => {
    if (!confirm("Delete this thread? All messages are lost.")) return;
    try {
      await deleteThread(id);
      if (activeThreadId === id) {
        setActiveThreadId(null);
        setMessages([]);
      }
      await loadThreads();
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <main className="px-8 py-10 max-w-6xl mx-auto">
      <Header />

      <div className="flex items-baseline gap-3 mb-2">
        <Link
          href={`/agents/${encodeURIComponent(agentName)}`}
          className="text-sm text-gray-500 hover:text-accent-600 transition-colors"
        >
          ← {agentName}
        </Link>
      </div>
      <h1 className="text-2xl font-semibold tracking-tight mb-1">Chat</h1>
      <p className="text-sm text-gray-500 mb-6">
        Talk to{" "}
        <span className="font-mono text-gray-800">{agentName}</span>. Each
        thread keeps its own history. Your bot needs an{" "}
        <code className="font-mono">@lightsei.on_chat</code> handler.
      </p>

      {error && (
        <div className="mb-4 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      <div className="grid grid-cols-12 gap-6 min-h-[28rem]">
        {/* Thread list */}
        <aside className="col-span-3 border border-gray-200 rounded-lg overflow-hidden flex flex-col">
          <button
            type="button"
            onClick={onNewThread}
            className="px-4 py-2.5 text-left text-sm font-medium text-accent-700 hover:bg-accent-50 border-b border-gray-200 transition-colors"
          >
            + new thread
          </button>
          <div className="flex-1 overflow-y-auto">
            {threads.length === 0 ? (
              <div className="p-4 text-xs text-gray-400 italic">
                no threads yet
              </div>
            ) : (
              threads.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  onClick={() => setActiveThreadId(t.id)}
                  className={
                    "w-full text-left px-4 py-2.5 text-sm border-b border-gray-100 transition-colors group " +
                    (activeThreadId === t.id
                      ? "bg-accent-50 text-accent-900"
                      : "hover:bg-gray-50 text-gray-800")
                  }
                >
                  <div className="flex items-center justify-between">
                    <span className="truncate">{t.title}</span>
                    <span
                      role="button"
                      tabIndex={0}
                      aria-label="delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        onDeleteThread(t.id);
                      }}
                      className="ml-2 text-gray-400 hover:text-red-600 opacity-0 group-hover:opacity-100 transition-opacity text-xs"
                    >
                      ×
                    </span>
                  </div>
                  <div className="text-[10px] text-gray-400 mt-0.5 font-mono">
                    {fmtTime(t.updated_at)}
                  </div>
                </button>
              ))
            )}
          </div>
        </aside>

        {/* Chat area */}
        <section className="col-span-9 border border-gray-200 rounded-lg flex flex-col">
          {!activeThreadId ? (
            <div className="flex-1 flex items-center justify-center text-sm text-gray-400">
              {threads.length === 0
                ? "click + new thread to start"
                : "pick a thread on the left"}
            </div>
          ) : (
            <>
              <div
                ref={messagesRef}
                className="flex-1 overflow-y-auto p-5 space-y-4 max-h-[32rem]"
              >
                {messages.length === 0 && (
                  <div className="text-sm text-gray-400 italic">
                    say something…
                  </div>
                )}
                {messages.map((m) => {
                  const isPending = m.status === "pending" || m.status === "in_progress";
                  const isFailed = m.status === "failed";
                  return (
                    <div key={m.id} className="space-y-1">
                      <div className="text-[10px] uppercase tracking-wider text-gray-400 font-semibold">
                        {m.role}
                      </div>
                      <div
                        className={
                          "p-3.5 border rounded-lg text-sm whitespace-pre-wrap break-words " +
                          (isFailed
                            ? "bg-red-50 border-red-200 text-red-900"
                            : bubbleColors(m.role))
                        }
                      >
                        {isPending ? (
                          <span className="text-gray-400 italic">thinking…</span>
                        ) : isFailed ? (
                          <>
                            <div className="text-xs uppercase tracking-wider opacity-70 font-semibold mb-1">
                              error
                            </div>
                            {m.error}
                          </>
                        ) : (
                          m.content || (
                            <span className="text-gray-400 italic">(empty)</span>
                          )
                        )}
                      </div>
                    </div>
                  );
                })}
              </div>
              <form
                onSubmit={onSend}
                className="border-t border-gray-200 p-3 flex gap-2 items-end"
              >
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      onSend(e as unknown as React.FormEvent);
                    }
                  }}
                  placeholder="message your agent…"
                  rows={2}
                  className="flex-1 border border-gray-300 rounded-md px-3 py-2 text-sm resize-y focus:outline-none focus:ring-2 focus:ring-accent-500/30 focus:border-accent-500"
                />
                <button
                  type="submit"
                  disabled={sending || !draft.trim()}
                  className="px-4 py-2 bg-accent-600 hover:bg-accent-700 text-white rounded-md text-sm font-medium disabled:opacity-50 transition-colors"
                >
                  send
                </button>
              </form>
            </>
          )}
        </section>
      </div>
    </main>
  );
}
