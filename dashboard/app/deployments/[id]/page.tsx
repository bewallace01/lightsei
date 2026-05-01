"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import {
  Deployment,
  DeploymentLogLine,
  fetchDeployment,
  fetchDeploymentLogs,
  redeployDeployment,
  stopDeployment,
  UnauthorizedError,
} from "../../api";
import Header from "../../Header";

function fmt(iso: string | null): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

function statusClass(status: Deployment["status"]): string {
  switch (status) {
    case "running":
      return "bg-green-100 text-green-800";
    case "failed":
      return "bg-red-100 text-red-800";
    case "stopped":
      return "bg-gray-100 text-gray-700";
    case "building":
      return "bg-amber-100 text-amber-800";
    case "queued":
      return "bg-blue-100 text-blue-800";
  }
}

function streamColor(stream: DeploymentLogLine["stream"]): string {
  switch (stream) {
    case "stdout":
      return "text-gray-100";
    case "stderr":
      return "text-amber-300";
    case "system":
      return "text-blue-300";
  }
}

export default function DeploymentDetailPage({
  params,
}: {
  params: { id: string };
}) {
  const id = params.id;
  const router = useRouter();
  const [dep, setDep] = useState<Deployment | null>(null);
  const [lines, setLines] = useState<DeploymentLogLine[]>([]);
  const [maxId, setMaxId] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const tailRef = useRef<HTMLDivElement | null>(null);

  // Adaptive polling interval: faster while building/running, slower otherwise.
  const pollMs = (() => {
    if (!dep) return 1500;
    if (dep.status === "building" || dep.status === "running") return 1500;
    if (dep.status === "queued") return 2000;
    return 5000;
  })();

  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const [d, l] = await Promise.all([
          fetchDeployment(id),
          fetchDeploymentLogs(id, maxId, 500),
        ]);
        if (cancelled) return;
        setDep(d);
        if (l.lines.length) {
          setLines((prev) => [...prev, ...l.lines]);
          setMaxId(l.max_id);
        }
        setError(null);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof UnauthorizedError) {
          router.replace("/login");
          return;
        }
        setError(String(e));
      }
    };
    tick();
    const interval = setInterval(tick, pollMs);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, maxId, pollMs]);

  useEffect(() => {
    if (!autoScroll) return;
    if (tailRef.current) {
      tailRef.current.scrollTop = tailRef.current.scrollHeight;
    }
  }, [lines, autoScroll]);

  const onStop = async () => {
    if (!dep || !confirm("Stop this deployment?")) return;
    try {
      const updated = await stopDeployment(dep.id);
      setDep(updated);
    } catch (e) {
      setError(String(e));
    }
  };

  const onRedeploy = async () => {
    if (!dep || !confirm("Redeploy from this bundle?")) return;
    try {
      const created = await redeployDeployment(dep.id);
      router.push(`/deployments/${created.id}`);
    } catch (e) {
      setError(String(e));
    }
  };

  return (
    <main className="px-8 py-10 max-w-5xl mx-auto">
      <Header />

      <div className="mb-6">
        <Link
          href={
            dep
              ? `/agents/${encodeURIComponent(dep.agent_name)}`
              : "/"
          }
          className="text-sm text-gray-500 hover:text-accent-600 transition-colors"
        >
          ← {dep?.agent_name ?? "agent"}
        </Link>
      </div>

      <div className="flex items-center gap-3 mb-2">
        <h1 className="text-2xl font-semibold tracking-tight">
          deployment <span className="font-mono">{id.slice(0, 8)}…</span>
        </h1>
        {dep && (
          <span
            className={
              "inline-block px-2 py-0.5 rounded-full text-[11px] font-medium uppercase tracking-wider " +
              statusClass(dep.status)
            }
          >
            {dep.status}
          </span>
        )}
      </div>

      {error && (
        <div className="mb-4 p-3 border border-red-200 bg-red-50 text-red-700 text-sm rounded-md">
          {error}
        </div>
      )}

      {dep && (
        <section className="mb-6 grid grid-cols-2 gap-x-8 gap-y-2 text-sm border border-gray-200 rounded-lg p-4">
          <div className="text-gray-500">agent</div>
          <div className="font-mono">{dep.agent_name}</div>

          <div className="text-gray-500">desired</div>
          <div className="font-mono">{dep.desired_state}</div>

          <div className="text-gray-500">created</div>
          <div className="font-mono">{fmt(dep.created_at)}</div>

          <div className="text-gray-500">started</div>
          <div className="font-mono">{fmt(dep.started_at)}</div>

          <div className="text-gray-500">stopped</div>
          <div className="font-mono">{fmt(dep.stopped_at)}</div>

          <div className="text-gray-500">claimed by</div>
          <div className="font-mono text-xs truncate">
            {dep.claimed_by ?? "—"}
          </div>

          <div className="text-gray-500">source</div>
          <div className="font-mono">
            {dep.source === "github_push" ? (
              <span className="inline-flex items-center gap-1">
                <span>github push</span>
                {dep.source_commit_sha && (
                  <span
                    className="text-gray-500"
                    title={dep.source_commit_sha}
                  >
                    @ {dep.source_commit_sha.slice(0, 7)}
                  </span>
                )}
              </span>
            ) : (
              <span>cli upload</span>
            )}
          </div>

          {dep.error && (
            <>
              <div className="text-gray-500">error</div>
              <div className="font-mono text-red-700 whitespace-pre-wrap">
                {dep.error}
              </div>
            </>
          )}
        </section>
      )}

      {dep && (
        <div className="mb-4 flex items-center gap-3">
          {(dep.status === "running" ||
            dep.status === "building" ||
            dep.status === "queued") && (
            <button
              type="button"
              onClick={onStop}
              className="px-3 py-1.5 text-sm border border-red-300 text-red-700 rounded-md hover:bg-red-50"
            >
              stop
            </button>
          )}
          {(dep.status === "stopped" || dep.status === "failed") &&
            dep.source_blob_id && (
              <button
                type="button"
                onClick={onRedeploy}
                className="px-3 py-1.5 text-sm bg-accent-600 hover:bg-accent-700 text-white rounded-md"
              >
                redeploy
              </button>
            )}
          <label className="ml-auto flex items-center gap-2 text-xs text-gray-500">
            <input
              type="checkbox"
              checked={autoScroll}
              onChange={(e) => setAutoScroll(e.target.checked)}
            />
            auto-scroll
          </label>
        </div>
      )}

      <section>
        <h2 className="text-[11px] font-semibold text-gray-500 mb-3 uppercase tracking-wider">
          Logs
        </h2>
        <div
          ref={tailRef}
          className="bg-gray-900 text-gray-100 font-mono text-xs rounded-lg p-4 h-[28rem] overflow-y-auto whitespace-pre-wrap"
        >
          {lines.length === 0 ? (
            <span className="text-gray-500 italic">
              waiting for log lines…
            </span>
          ) : (
            lines.map((l) => (
              <div key={l.id} className={streamColor(l.stream)}>
                <span className="text-gray-500 mr-2">
                  {new Date(l.ts).toLocaleTimeString()}
                </span>
                <span className="text-gray-500 mr-2">[{l.stream}]</span>
                {l.line}
              </div>
            ))
          )}
        </div>
        <div className="mt-2 text-[11px] text-gray-400 font-mono">
          {lines.length} line(s) · capped at most-recent 1000 server-side
        </div>
      </section>
    </main>
  );
}
