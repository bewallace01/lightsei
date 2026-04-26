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
    <main className="px-8 py-10 max-w-6xl mx-auto">
      <Header />

      <Link
        href="/"
        className="text-sm text-gray-500 hover:text-accent-600 transition-colors inline-block mb-4"
      >
        ← runs
      </Link>

      <h1 className="text-lg font-semibold tracking-tight font-mono text-gray-900 mb-1">
        {runId}
      </h1>
      {run && (
        <div className="text-sm text-gray-500 mb-8">
          agent <span className="font-mono text-gray-700">{run.agent_name}</span>
          <span className="text-gray-300 mx-2">·</span>
          started {fmtTime(run.started_at)}
          {run.ended_at ? (
            <>
              <span className="text-gray-300 mx-2">·</span>
              ended {fmtTime(run.ended_at)}
            </>
          ) : (
            <>
              <span className="text-gray-300 mx-2">·</span>
              <span className="text-amber-700">running</span>
            </>
          )}
        </div>
      )}

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
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
          <div className="mb-8 p-5 border border-red-200 bg-red-50 rounded-lg">
            <div className="flex items-baseline gap-3">
              <span className="px-2 py-0.5 rounded-full bg-red-200 text-red-900 text-[11px] font-semibold uppercase tracking-wide">
                denied
              </span>
              <span className="text-red-900 font-medium">
                {p.reason ?? "policy denied"}
              </span>
            </div>
            <div className="mt-3 text-sm text-red-800 space-y-1">
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
                    cost so far{" "}
                    <span className="font-mono">
                      ${p.cost_so_far_usd.toFixed(6)}
                    </span>
                    , cap{" "}
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
          <section className="mb-10">
            <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
              Conversation
            </h2>
            <div className="space-y-7">
              {turns.map((t, i) => (
                <div key={i} className="space-y-2.5">
                  <div className="text-xs text-gray-500 flex items-center gap-2">
                    <span>
                      call {i + 1}
                      {t.model && (
                        <>
                          <span className="text-gray-300 mx-1.5">·</span>
                          <span className="font-mono text-gray-700">
                            {t.model}
                          </span>
                        </>
                      )}
                    </span>
                    {t.denied && (
                      <span className="px-1.5 py-0.5 rounded-full bg-red-100 text-red-800 text-[10px] uppercase font-semibold tracking-wide">
                        denied{t.denied.reason ? ` · ${t.denied.reason}` : ""}
                      </span>
                    )}
                  </div>
                  {t.request.map((m, j) => (
                    <div
                      key={`req-${j}`}
                      className={
                        "p-3.5 border rounded-lg text-sm whitespace-pre-wrap break-words " +
                        bubbleColors(m.role)
                      }
                    >
                      <div className="text-[10px] uppercase tracking-wider opacity-60 font-semibold mb-1.5">
                        {m.role ?? "?"}
                      </div>
                      {messageText(m)}
                    </div>
                  ))}
                  {t.response !== undefined && (
                    <div
                      className={
                        "p-3.5 border rounded-lg text-sm whitespace-pre-wrap break-words " +
                        bubbleColors("assistant")
                      }
                    >
                      <div className="text-[10px] uppercase tracking-wider opacity-60 font-semibold mb-1.5">
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

      <h2 className="text-[11px] font-semibold text-gray-500 mb-4 uppercase tracking-wider">
        Events
      </h2>

      {loading ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : events.length === 0 ? (
        <div className="text-gray-400 text-sm">no events</div>
      ) : (
        <div className="rounded-lg border border-gray-200 overflow-hidden">
        <table className="w-full text-left text-sm">
          <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
            <tr>
              <th className="px-4 py-3 font-medium w-44">Time</th>
              <th className="px-4 py-3 font-medium w-52">Kind</th>
              <th className="px-4 py-3 font-medium">Payload</th>
            </tr>
          </thead>
          <tbody>
            {events.map((e, i) => {
              const isDenial = e.kind === "policy_denied";
              return (
                <tr
                  key={e.id}
                  className={
                    "align-top " +
                    (isDenial
                      ? "bg-red-50/50 "
                      : "") +
                    (i !== events.length - 1
                      ? "border-b border-gray-100"
                      : "")
                  }
                >
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {fmtTime(e.timestamp)}
                  </td>
                  <td className="px-4 py-3 font-mono text-xs">
                    {isDenial ? (
                      <span className="text-red-800 font-semibold">
                        {e.kind}
                      </span>
                    ) : (
                      <span className="text-gray-700">{e.kind}</span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <pre
                      className={
                        "font-mono text-xs whitespace-pre-wrap break-words " +
                        (isDenial ? "text-red-900" : "text-gray-700")
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
        </div>
      )}
    </main>
  );
}
