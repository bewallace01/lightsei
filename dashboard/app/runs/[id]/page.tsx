"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Event, fetchRunEvents, Run, UnauthorizedError } from "../../api";
import Header from "../../Header";

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

type Message = { role?: string; content?: string | unknown };

type Turn = {
  model?: string;
  request: Message[];
  response?: string;
  denied?: { reason?: string };
};

function buildConversation(events: Event[]): Turn[] {
  const turns: Turn[] = [];
  let current: Turn | null = null;
  for (const e of events) {
    const p = (e.payload || {}) as Record<string, unknown>;
    if (e.kind === "llm_call_started") {
      const reqMsgs = p.request_messages as Message[] | undefined;
      current = {
        model: (p.model as string | undefined) ?? undefined,
        request: Array.isArray(reqMsgs) ? reqMsgs : [],
      };
      turns.push(current);
    } else if (e.kind === "llm_call_completed" && current) {
      if (!current.model && p.model) current.model = p.model as string;
      if (typeof p.response_content === "string") {
        current.response = p.response_content;
      }
      current = null;
    } else if (e.kind === "llm_call_failed" && current) {
      current = null;
    } else if (e.kind === "policy_denied") {
      // Denial happens before llm_call_started, so the request_messages live
      // on the denial event itself when the SDK captured them.
      const reqMsgs = p.request_messages as Message[] | undefined;
      if (Array.isArray(reqMsgs) && reqMsgs.length > 0) {
        turns.push({
          model: (p.model as string | undefined) ?? undefined,
          request: reqMsgs,
          denied: { reason: p.reason as string | undefined },
        });
      }
      current = null;
    }
  }
  return turns;
}

function bubbleColors(role: string | undefined): string {
  switch (role) {
    case "system":
      return "bg-gray-100 border-gray-200 text-gray-800";
    case "user":
      return "bg-blue-50 border-blue-200 text-blue-900";
    case "assistant":
      return "bg-green-50 border-green-200 text-green-900";
    case "tool":
      return "bg-purple-50 border-purple-200 text-purple-900";
    default:
      return "bg-gray-50 border-gray-200 text-gray-700";
  }
}

function messageText(m: Message): string {
  if (typeof m.content === "string") return m.content;
  // Anthropic-style content blocks; flatten any text parts
  if (Array.isArray(m.content)) {
    return (m.content as Array<{ type?: string; text?: string }>)
      .filter((b) => b.type === "text" && typeof b.text === "string")
      .map((b) => b.text)
      .join("");
  }
  return JSON.stringify(m.content ?? "");
}

export default function RunDetail({ params }: { params: { id: string } }) {
  const runId = params.id;
  const router = useRouter();
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await fetchRunEvents(runId);
        if (!alive) return;
        setRun(data.run);
        setEvents(data.events);
        setError(null);
      } catch (e) {
        if (!alive) return;
        if (e instanceof UnauthorizedError) {
          router.replace("/login");
          return;
        }
        setError(String(e));
      } finally {
        if (alive) setLoading(false);
      }
    };
    tick();
    const id = setInterval(tick, 2000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [runId, router]);

  return (
    <main className="p-8 max-w-6xl mx-auto">
      <Header />

      <div className="mb-4">
        <Link href="/" className="text-blue-600 underline text-sm">
          &larr; runs
        </Link>
      </div>

      <h1 className="text-xl font-semibold mb-1 font-mono">{runId}</h1>
      {run && (
        <div className="text-sm text-gray-600 mb-6">
          agent <span className="font-mono">{run.agent_name}</span>
          {" · started "}
          {fmtTime(run.started_at)}
          {run.ended_at ? ` · ended ${fmtTime(run.ended_at)}` : " · running"}
        </div>
      )}

      {error && (
        <div className="mb-4 p-3 border border-red-300 bg-red-50 text-red-700 text-sm rounded">
          {error}
        </div>
      )}

      {(() => {
        const denial = events.find((e) => e.kind === "policy_denied");
        if (!denial) return null;
        const p = denial.payload as {
          policy?: string;
          reason?: string;
          cap_usd?: number;
          cost_so_far_usd?: number;
          action?: string;
        };
        return (
          <div className="mb-6 p-4 border border-red-300 bg-red-50 rounded">
            <div className="flex items-baseline gap-3">
              <span className="px-2 py-0.5 rounded bg-red-200 text-red-900 text-xs font-semibold uppercase">
                denied
              </span>
              <span className="text-red-800 font-medium">
                {p.reason ?? "policy denied"}
              </span>
            </div>
            <div className="mt-2 text-sm text-red-800 space-y-0.5">
              {p.policy && (
                <div>
                  policy: <span className="font-mono">{p.policy}</span>
                </div>
              )}
              {p.action && (
                <div>
                  action: <span className="font-mono">{p.action}</span>
                </div>
              )}
              {typeof p.cost_so_far_usd === "number" &&
                typeof p.cap_usd === "number" && (
                  <div>
                    cost so far:{" "}
                    <span className="font-mono">
                      ${p.cost_so_far_usd.toFixed(6)}
                    </span>{" "}
                    / cap{" "}
                    <span className="font-mono">${p.cap_usd.toFixed(6)}</span>
                  </div>
                )}
            </div>
          </div>
        );
      })()}

      {(() => {
        const turns = buildConversation(events);
        const hasContent = turns.some(
          (t) => t.request.length > 0 || t.response !== undefined,
        );
        if (!hasContent) return null;
        return (
          <section className="mb-8">
            <h2 className="text-sm font-semibold text-gray-700 mb-3 uppercase tracking-wide">
              Conversation
            </h2>
            <div className="space-y-6">
              {turns.map((t, i) => (
                <div key={i} className="space-y-2">
                  <div className="text-xs text-gray-500 flex items-center gap-2">
                    <span>
                      call {i + 1}
                      {t.model && (
                        <>
                          {" · "}
                          <span className="font-mono">{t.model}</span>
                        </>
                      )}
                    </span>
                    {t.denied && (
                      <span className="px-1.5 py-0.5 rounded bg-red-100 text-red-800 text-[10px] uppercase font-semibold">
                        denied{t.denied.reason ? ` · ${t.denied.reason}` : ""}
                      </span>
                    )}
                  </div>
                  {t.request.map((m, j) => (
                    <div
                      key={`req-${j}`}
                      className={
                        "p-3 border rounded text-sm whitespace-pre-wrap break-words " +
                        bubbleColors(m.role)
                      }
                    >
                      <div className="text-xs uppercase tracking-wide opacity-70 mb-1">
                        {m.role ?? "?"}
                      </div>
                      {messageText(m)}
                    </div>
                  ))}
                  {t.response !== undefined && (
                    <div
                      className={
                        "p-3 border rounded text-sm whitespace-pre-wrap break-words " +
                        bubbleColors("assistant")
                      }
                    >
                      <div className="text-xs uppercase tracking-wide opacity-70 mb-1">
                        assistant
                      </div>
                      {t.response}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>
        );
      })()}

      {loading ? (
        <div className="text-gray-500">loading...</div>
      ) : events.length === 0 ? (
        <div className="text-gray-500">no events</div>
      ) : (
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-gray-200 text-gray-600">
              <th className="py-2 pr-4 font-medium w-40">Time</th>
              <th className="py-2 pr-4 font-medium w-48">Kind</th>
              <th className="py-2 pr-4 font-medium">Payload</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e) => {
              const isDenial = e.kind === "policy_denied";
              return (
                <tr
                  key={e.id}
                  className={
                    "border-b align-top " +
                    (isDenial
                      ? "border-red-200 bg-red-50"
                      : "border-gray-100")
                  }
                >
                  <td className="py-2 pr-4 font-mono text-xs">
                    {fmtTime(e.timestamp)}
                  </td>
                  <td className="py-2 pr-4 font-mono text-xs">
                    {isDenial ? (
                      <span className="text-red-800 font-semibold">
                        {e.kind}
                      </span>
                    ) : (
                      e.kind
                    )}
                  </td>
                  <td className="py-2 pr-4">
                    <pre
                      className={
                        "font-mono text-xs whitespace-pre-wrap break-words " +
                        (isDenial ? "text-red-900" : "text-gray-800")
                      }
                    >
                      {JSON.stringify(e.payload, null, 2)}
                    </pre>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </main>
  );
}
