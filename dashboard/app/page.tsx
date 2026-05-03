"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { fetchRunSummaries, RunSummary, UnauthorizedError } from "./api";
import Constellation from "./Constellation";
import CostPanel from "./CostPanel";
import Hero from "./Hero";

function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

export default function Home() {
  const router = useRouter();
  const [rows, setRows] = useState<RunSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const data = await fetchRunSummaries();
        if (!alive) return;
        setRows(data);
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
  }, [router]);

  return (
    <main className="px-8 py-10 max-w-6xl mx-auto">

      {/* Phase 11B.2 + 11B.3: hero text overlays the constellation
          canvas rather than stacking above it. The hero text sits
          absolutely positioned in the upper-left while the
          constellation map fills the entire wrapper, so the empty
          space next to the headline is filled by stars + agents
          instead of negative whitespace.
          The wrapper itself paints the gradient + warmth + scattered
          field stars so the whole frame reads as one continuous
          sky. */}
      <section
        className="mb-10 relative overflow-hidden rounded-lg border border-indigo-900/50 shadow-lg shadow-indigo-950/30"
        style={{
          background:
            "linear-gradient(180deg, #020617 0%, #1e1b4b 50%, #0f172a 100%)",
        }}
      >
        {/* Centered amber warmth radiating from where Polaris sits
            (constellation map middle). */}
        <div
          className="absolute inset-0 pointer-events-none"
          style={{
            background:
              "radial-gradient(closest-side at 50% 50%, rgba(251,191,36,0.07), rgba(251,191,36,0) 60%)",
          }}
        />
        {/* Field stars scattered across the whole frame — viewBox
            matches the constellation map's so positions stay
            consistent across the shared canvas. */}
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none"
          aria-hidden="true"
          preserveAspectRatio="none"
          viewBox="0 0 1000 480"
        >
          {[
            { x: 80, y: 30, r: 1, o: 0.6 },
            { x: 220, y: 60, r: 0.8, o: 0.4 },
            { x: 540, y: 25, r: 0.8, o: 0.4 },
            { x: 880, y: 40, r: 1.2, o: 0.5 },
            { x: 940, y: 110, r: 1, o: 0.4 },
            { x: 60, y: 130, r: 0.8, o: 0.5 },
            { x: 200, y: 150, r: 1, o: 0.5 },
            { x: 360, y: 150, r: 0.8, o: 0.4 },
            { x: 860, y: 145, r: 1, o: 0.5 },
            { x: 130, y: 230, r: 0.9, o: 0.5 },
            { x: 290, y: 265, r: 0.8, o: 0.4 },
            { x: 470, y: 240, r: 0.7, o: 0.45 },
            { x: 660, y: 265, r: 0.8, o: 0.45 },
            { x: 820, y: 230, r: 1, o: 0.55 },
            { x: 940, y: 265, r: 0.7, o: 0.4 },
            { x: 60, y: 340, r: 0.9, o: 0.5 },
            { x: 220, y: 370, r: 0.8, o: 0.4 },
            { x: 380, y: 385, r: 1, o: 0.55 },
            { x: 560, y: 350, r: 0.8, o: 0.4 },
            { x: 720, y: 370, r: 0.9, o: 0.5 },
            { x: 880, y: 385, r: 0.8, o: 0.45 },
            { x: 130, y: 445, r: 0.9, o: 0.55 },
            { x: 280, y: 460, r: 0.8, o: 0.4 },
            { x: 460, y: 440, r: 0.9, o: 0.5 },
            { x: 640, y: 455, r: 0.8, o: 0.45 },
            { x: 820, y: 450, r: 0.9, o: 0.5 },
          ].map((d, i) => (
            <circle
              key={i}
              cx={d.x}
              cy={d.y}
              r={d.r}
              fill="white"
              opacity={d.o}
            />
          ))}
        </svg>

        {/* The constellation map fills the canvas. Behind the hero
            text overlay, but pointer events still work for hover. */}
        <Constellation />

        {/* Hero text + constellation label, positioned absolutely
            in the upper-left so they overlay the dark canvas rather
            than reserving their own vertical space above it. The
            pointer-events-none lets the constellation map's stars
            stay clickable through the hero's transparent areas; the
            inner content keeps pointer events on so the headline's
            expand button still works. */}
        <div className="absolute inset-x-0 top-0 pointer-events-none">
          <div className="max-w-md pointer-events-auto">
            <Hero />
            <div className="px-6 sm:px-10">
              <div className="flex items-baseline gap-3">
                <span className="text-[11px] uppercase tracking-[0.18em] text-indigo-200/85 font-medium">
                  Constellation
                </span>
                <span className="text-xs text-indigo-200/45">
                  your team, at a glance
                </span>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Phase 11B.4: cost panel. Light card under the shared sky,
          bordered in the same indigo accent so it ties to the
          constellation map without competing for visual focus. */}
      <section className="mb-10">
        <CostPanel />
      </section>

      <div className="flex items-baseline justify-between mb-8">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Runs</h1>
          <p className="text-sm text-gray-500 mt-1">
            Every call your agents made, newest first.
          </p>
        </div>
        <span className="text-xs text-gray-400">refreshes every 2s</span>
      </div>

      {error && (
        <div className="mb-6 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {loading ? (
        <div className="text-gray-400 text-sm">loading…</div>
      ) : rows.length === 0 ? (
        <div className="border border-dashed border-gray-200 rounded-lg p-10 text-center">
          <div className="text-gray-700 font-medium mb-1">No runs yet</div>
          <p className="text-sm text-gray-500">
            Make your first OpenAI or Anthropic call with the SDK initialized,
            then come back here.
          </p>
        </div>
      ) : (
        <div className="rounded-lg border border-gray-200 overflow-hidden">
          <table className="w-full text-left text-sm">
            <thead className="bg-gray-50 text-[11px] uppercase tracking-wider text-gray-500">
              <tr>
                <th className="px-4 py-3 font-medium">Started</th>
                <th className="px-4 py-3 font-medium">Agent</th>
                <th className="px-4 py-3 font-medium">Model</th>
                <th className="px-4 py-3 font-medium">Events</th>
                <th className="px-4 py-3 font-medium">Tokens</th>
                <th className="px-4 py-3 font-medium">Latency</th>
                <th className="px-4 py-3 font-medium">Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r, i) => (
                <tr
                  key={r.id}
                  className={
                    "hover:bg-gray-50 transition-colors " +
                    (i !== rows.length - 1 ? "border-b border-gray-100" : "")
                  }
                >
                  <td className="px-4 py-3">
                    <Link
                      href={`/runs/${r.id}`}
                      className="text-accent-600 hover:text-accent-700 font-medium"
                    >
                      {fmtTime(r.started_at)}
                    </Link>
                  </td>
                  <td className="px-4 py-3 text-gray-800">
                    <Link
                      href={`/agents/${encodeURIComponent(r.agent_name)}`}
                      className="hover:text-accent-600 transition-colors"
                    >
                      {r.agent_name}
                    </Link>
                  </td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {r.model ?? "—"}
                  </td>
                  <td className="px-4 py-3 text-gray-700">{r.event_count}</td>
                  <td className="px-4 py-3 font-mono text-xs text-gray-600">
                    {r.input_tokens} / {r.output_tokens}
                  </td>
                  <td className="px-4 py-3 text-gray-700">
                    {r.latency_ms > 0 ? `${r.latency_ms} ms` : "—"}
                  </td>
                  <td className="px-4 py-3">
                    {r.denied ? (
                      <span
                        className="inline-block px-2 py-0.5 rounded-full bg-red-100 text-red-800 text-[11px] font-medium"
                        title={r.denial?.reason ?? "policy denied"}
                      >
                        denied
                      </span>
                    ) : r.ended_at ? (
                      <span className="inline-block px-2 py-0.5 rounded-full bg-gray-100 text-gray-700 text-[11px] font-medium">
                        ended
                      </span>
                    ) : (
                      <span className="inline-block px-2 py-0.5 rounded-full bg-amber-100 text-amber-800 text-[11px] font-medium">
                        running
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
