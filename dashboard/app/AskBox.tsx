"use client";

import { useRef, useState } from "react";
import {
  AskAnswer,
  askBusinessTeam,
  fetchAnswer,
} from "./api";

// How long to keep polling for an answer before giving up (the BI
// assistant usually replies within a few seconds; this guards against a
// never-deployed assistant leaving the box spinning forever).
const POLL_MS = 2000;
const MAX_POLLS = 30;

/**
 * "Ask your business team" — the vision's chat-first surface. The owner
 * types a plain-English question; it routes to the BI assistant's
 * question-mode and the answer renders inline once it lands.
 */
export default function AskBox() {
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [answer, setAnswer] = useState<AskAnswer | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const SUGGESTIONS = [
    "How did we do this week?",
    "Any reviews I should worry about?",
    "What needs my attention today?",
  ];

  function stopPolling() {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
  }

  async function ask(q: string) {
    const text = q.trim();
    if (!text || asking) return;
    setAsking(true);
    setAnswer({ status: "pending", question: text });
    setNote(null);
    stopPolling();

    try {
      const { command_id, bi_assistant_deployed } = await askBusinessTeam(text);
      if (!bi_assistant_deployed) {
        setNote(
          "Your Business Intelligence assistant isn't online yet, so this " +
            "may take a moment (or set it up from the welcome page).",
        );
      }
      let polls = 0;
      const poll = async () => {
        polls += 1;
        try {
          const res = await fetchAnswer(command_id);
          if (res.status === "pending") {
            if (polls >= MAX_POLLS) {
              setAnswer({
                status: "failed",
                question: text,
                error: "still thinking — check back in a moment.",
              });
              setAsking(false);
              return;
            }
            timer.current = setTimeout(poll, POLL_MS);
            return;
          }
          setAnswer(res);
          setAsking(false);
        } catch {
          setAnswer({
            status: "failed",
            question: text,
            error: "couldn't reach your team just now.",
          });
          setAsking(false);
        }
      };
      timer.current = setTimeout(poll, POLL_MS);
    } catch (e) {
      setAnswer({ status: "failed", question: text, error: String(e) });
      setAsking(false);
    }
  }

  return (
    <section className="mb-6 sm:mb-10">
      <div className="rounded-lg border border-gray-200 p-4 sm:p-5 bg-white">
        <div className="flex items-center gap-2">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") ask(question);
            }}
            placeholder="Ask your business team…"
            className="flex-1 text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-indigo-600"
          />
          <button
            onClick={() => ask(question)}
            disabled={asking || !question.trim()}
            className="text-sm px-4 py-2 rounded-md bg-accent-600 text-white hover:bg-accent-700 disabled:opacity-50 whitespace-nowrap"
          >
            {asking ? "Asking…" : "Ask"}
          </button>
        </div>

        {!answer && (
          <div className="mt-3 flex flex-wrap gap-2">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                onClick={() => {
                  setQuestion(s);
                  ask(s);
                }}
                className="text-xs px-2.5 py-1 rounded-full border border-gray-200 text-gray-600 hover:border-gray-300 hover:bg-gray-50"
              >
                {s}
              </button>
            ))}
          </div>
        )}

        {answer && (
          <div className="mt-4 border-t border-gray-100 pt-4">
            <div className="text-xs text-gray-400 mb-1">
              You asked: {answer.question}
            </div>
            {answer.status === "pending" ? (
              <div className="text-sm text-gray-400">
                Your team is looking into it…
              </div>
            ) : answer.status === "failed" ? (
              <div className="text-sm text-amber-700">{answer.error}</div>
            ) : (
              <div className="text-sm text-gray-900 leading-relaxed whitespace-pre-wrap">
                {answer.answer}
              </div>
            )}
            {note && answer.status === "pending" && (
              <div className="mt-2 text-xs text-gray-400">{note}</div>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
