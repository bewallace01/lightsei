"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  GithubRepo,
  PageFormat,
  SeoAuditState,
  SeoDraft,
  fetchGithubConnection,
  fetchSeoAudit,
  fetchSeoDrafts,
  generateSeoPage,
  handleAuthError,
  publishPage,
  runSeoAudit,
} from "../api";

/** "Site health" — the latest SEO audit Spica ran on the owner's site, with
 * a score, the prioritized findings, and an "audit now" button. This is the
 * visible face of the always-on audit feeder. */
function SiteHealthPanel({ audit }: { audit: SeoAuditState | null }) {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [local, setLocal] = useState<SeoAuditState | null>(audit);

  useEffect(() => setLocal(audit), [audit]);

  async function refresh() {
    try {
      setLocal(await fetchSeoAudit());
    } catch {
      /* best-effort */
    }
  }

  async function onAuditNow() {
    setBusy(true);
    setNote(null);
    try {
      await runSeoAudit();
      setNote("Spica is auditing your site. The score updates in a moment.");
      setTimeout(refresh, 6000);
      setTimeout(refresh, 14000);
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  }

  const latest = local?.latest ?? null;
  const url = local?.configured_url ?? latest?.url ?? null;
  const score = latest?.score ?? null;
  const scoreColor =
    score == null ? "text-gray-400"
      : score >= 85 ? "text-emerald-600"
      : score >= 60 ? "text-amber-600"
      : "text-red-600";
  const bad = (latest?.findings ?? []).filter((f) => f.status !== "good");

  return (
    <div className="rounded-lg border border-gray-200 p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-medium text-gray-900">Site health</div>
          <div className="text-xs text-gray-500 mt-0.5">
            {url ? <span className="font-mono">{url}</span> : "No site set yet — add one in feeder settings."}
          </div>
        </div>
        <div className="text-right">
          {score != null && (
            <div className={"text-2xl font-semibold " + scoreColor}>{score}<span className="text-sm text-gray-400">/100</span></div>
          )}
          <button
            onClick={onAuditNow}
            disabled={busy}
            className="mt-1 text-xs px-2.5 py-1 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
          >
            {busy ? "Auditing…" : "Audit now"}
          </button>
        </div>
      </div>
      {note && <p className="mt-2 text-xs text-gray-500">{note}</p>}
      {latest && bad.length > 0 && (
        <ul className="mt-3 border-t border-gray-100 pt-3 space-y-1.5">
          {bad.map((f, i) => (
            <li key={i} className="text-sm flex gap-2">
              <span className={f.status === "issue" ? "text-red-500" : "text-amber-500"}>
                {f.status === "issue" ? "●" : "○"}
              </span>
              <span className="text-gray-700">
                <span className="font-medium">{f.check.replace(/_/g, " ")}:</span> {f.detail}
              </span>
            </li>
          ))}
        </ul>
      )}
      {latest && bad.length === 0 && (
        <p className="mt-3 text-sm text-emerald-700 border-t border-gray-100 pt-3">
          No issues found — your on-page SEO looks clean.
        </p>
      )}
    </div>
  );
}

const PAGE_TYPES = ["landing", "service", "location", "blog"];

// Format -> {label, default repo path} (the backend renders the file; the
// path here mirrors its default so the owner sees + can tweak where it lands).
const FORMATS: { value: PageFormat; label: string; path: (slug: string) => string }[] = [
  { value: "html", label: "HTML (static site)", path: (s) => `public/pages/${s}.html` },
  { value: "markdown", label: "Markdown (Hugo, Astro, Jekyll, Eleventy)", path: (s) => `content/${s}.md` },
  { value: "mdx", label: "MDX (Next.js, Astro)", path: (s) => `src/content/${s}.mdx` },
];

/** "Ask Spica to write a page" — enqueues a generate command, then nudges a
 * refetch so the new draft appears below. */
function GeneratePanel({ onRequested }: { onRequested: () => void }) {
  const [keyword, setKeyword] = useState("");
  const [pageType, setPageType] = useState("landing");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function onGenerate() {
    if (!keyword.trim()) return;
    setBusy(true);
    setNote(null);
    try {
      const res = await generateSeoPage({ keyword: keyword.trim(), page_type: pageType });
      setKeyword("");
      setNote(
        res.seo_assistant_deployed
          ? "Spica is writing the page. It'll appear below in a moment."
          : "Queued, but the SEO assistant isn't deployed yet — add it from your team page.",
      );
      // Give the worker a head start, then refresh drafts.
      setTimeout(onRequested, 6000);
      setTimeout(onRequested, 15000);
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-gray-200 p-5">
      <div className="text-sm font-medium text-gray-900">Write a new SEO page</div>
      <div className="text-xs text-gray-500 mt-0.5">
        Give Spica a target keyword and it drafts a full, optimized page.
      </div>
      <div className="mt-3 flex flex-col sm:flex-row gap-2">
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && keyword.trim()) onGenerate();
          }}
          placeholder="e.g. emergency plumber in Austin"
          className="flex-1 text-sm rounded-md ring-1 ring-gray-300 px-3 py-2 focus:outline-none focus:ring-2 focus:ring-accent-600"
        />
        <select
          value={pageType}
          onChange={(e) => setPageType(e.target.value)}
          className="text-sm rounded-md ring-1 ring-gray-300 px-2 py-2 focus:outline-none focus:ring-2 focus:ring-accent-600"
        >
          {PAGE_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>
        <button
          onClick={onGenerate}
          disabled={busy || !keyword.trim()}
          className="text-sm px-4 py-2 rounded-md bg-accent-600 text-white hover:bg-accent-700 disabled:opacity-50"
        >
          {busy ? "Sending…" : "Write the page"}
        </button>
      </div>
      {note && <p className="mt-2 text-xs text-gray-500">{note}</p>}
    </div>
  );
}

type PublishState = {
  format: PageFormat;
  path: string;
  pathEdited: boolean;
  repoId: string;
  busy: boolean;
  result?: { pr_url: string; branch: string };
  error?: string;
};

function DraftCard({
  draft,
  repos,
}: {
  draft: SeoDraft;
  repos: GithubRepo[];
}) {
  const slug = draft.page.slug || "page";
  const [st, setSt] = useState<PublishState>({
    format: "html",
    path: FORMATS[0].path(slug),
    pathEdited: false,
    repoId: repos[0]?.id ?? "",
    busy: false,
  });
  const [open, setOpen] = useState(false);

  function onFormat(format: PageFormat) {
    const def = FORMATS.find((f) => f.value === format)!.path(slug);
    // Re-default the path when the owner hasn't hand-edited it.
    setSt((s) => ({ ...s, format, path: s.pathEdited ? s.path : def }));
  }

  async function onPublish() {
    if (!st.repoId) return;
    setSt((s) => ({ ...s, busy: true, error: undefined, result: undefined }));
    try {
      const res = await publishPage({
        repo_id: st.repoId,
        title: draft.page.title || draft.page.h1 || "New page",
        page: draft.page,
        format: st.format,
        // Send the path only if the owner customized it; otherwise let the
        // backend use the format's default.
        ...(st.pathEdited ? { path: st.path.trim() } : {}),
      });
      setSt((s) => ({ ...s, busy: false, result: { pr_url: res.pr_url, branch: res.branch } }));
    } catch (e) {
      setSt((s) => ({ ...s, busy: false, error: String(e) }));
    }
  }

  return (
    <div className="rounded-lg border border-gray-200 p-5">
      <div className="text-[11px] font-semibold text-gray-400 uppercase tracking-wider">
        {draft.keyword ? `Target: ${draft.keyword}` : "Drafted page"}
      </div>
      <h3 className="text-base font-semibold text-gray-900 mt-1">{draft.page.h1}</h3>
      <div className="text-xs text-gray-500 mt-1">
        <span className="font-mono">{draft.page.title}</span>
      </div>
      <p className="text-sm text-gray-600 mt-2">{draft.page.meta_description}</p>
      <div
        className="prose prose-sm max-w-none mt-3 text-sm text-gray-700 border-t border-gray-100 pt-3 max-h-48 overflow-y-auto"
        dangerouslySetInnerHTML={{ __html: draft.page.body_html || "" }}
      />

      {!open ? (
        <button
          onClick={() => setOpen(true)}
          className="mt-4 text-sm px-3 py-1.5 rounded-md bg-accent-600 text-white hover:bg-accent-700"
        >
          Publish to my site →
        </button>
      ) : st.result ? (
        <div className="mt-4 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-900">
          ✅ Opened a pull request on <span className="font-mono">{st.result.branch}</span>.{" "}
          <a href={st.result.pr_url} target="_blank" rel="noreferrer" className="underline font-medium">
            Review &amp; merge the PR
          </a>{" "}
          — your host (Vercel / Cloudflare / Railway) deploys it on merge.
        </div>
      ) : (
        <div className="mt-4 rounded-md border border-gray-200 p-3 space-y-2">
          <label className="block text-xs text-gray-500">
            Repository
            <select
              value={st.repoId}
              onChange={(e) => setSt((s) => ({ ...s, repoId: e.target.value }))}
              className="mt-1 w-full text-sm rounded-md ring-1 ring-gray-300 px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent-600"
            >
              {repos.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.repo_owner}/{r.repo_name} ({r.branch})
                </option>
              ))}
            </select>
          </label>
          <label className="block text-xs text-gray-500">
            Format
            <select
              value={st.format}
              onChange={(e) => onFormat(e.target.value as PageFormat)}
              className="mt-1 w-full text-sm rounded-md ring-1 ring-gray-300 px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent-600"
            >
              {FORMATS.map((f) => (
                <option key={f.value} value={f.value}>
                  {f.label}
                </option>
              ))}
            </select>
            <span className="text-[11px] text-gray-400">
              Pick what your site uses. Spica renders the page in that format.
            </span>
          </label>
          <label className="block text-xs text-gray-500">
            File path in repo
            <input
              value={st.path}
              onChange={(e) => setSt((s) => ({ ...s, path: e.target.value, pathEdited: true }))}
              className="mt-1 w-full text-sm font-mono rounded-md ring-1 ring-gray-300 px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent-600"
            />
            <span className="text-[11px] text-gray-400">
              Defaulted for the format above; tweak it to match your repo layout.
            </span>
          </label>
          {st.error && <div className="text-xs text-red-600">{st.error}</div>}
          <div className="flex gap-2 pt-1">
            <button
              onClick={onPublish}
              disabled={st.busy || !st.repoId || !st.path.trim()}
              className="text-sm px-3 py-1.5 rounded-md bg-accent-600 text-white hover:bg-accent-700 disabled:opacity-50"
            >
              {st.busy ? "Opening PR…" : "Open a pull request"}
            </button>
            <button
              onClick={() => setOpen(false)}
              className="text-sm px-3 py-1.5 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function SeoPage() {
  const router = useRouter();
  const [drafts, setDrafts] = useState<SeoDraft[] | null>(null);
  const [repos, setRepos] = useState<GithubRepo[]>([]);
  const [audit, setAudit] = useState<SeoAuditState | null>(null);
  const [githubConnected, setGithubConnected] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refetchDrafts() {
    try {
      setDrafts(await fetchSeoDrafts());
    } catch {
      /* best-effort refresh */
    }
  }

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [d, gh, a] = await Promise.all([
          fetchSeoDrafts(),
          fetchGithubConnection(),
          fetchSeoAudit().catch(() => null),
        ]);
        if (!alive) return;
        setDrafts(d);
        setRepos(gh.repos.filter((r) => r.is_active));
        setGithubConnected(gh.connection !== null);
        setAudit(a);
      } catch (e) {
        if (!alive) return;
        if (handleAuthError(e, router)) return;
        setError(String(e));
      }
    })();
    return () => {
      alive = false;
    };
  }, [router]);

  return (
    <main className="px-4 py-10 max-w-3xl mx-auto">
      <h1 className="text-2xl font-semibold tracking-tight">SEO · Spica</h1>
      <p className="text-sm text-gray-500 mt-1">
        Spica audits your site and drafts new SEO pages. Publish a draft and it
        opens a pull request on your repo — your host deploys it on merge.
      </p>

      {error && (
        <div className="mt-4 text-sm text-red-700 border border-red-200 bg-red-50 p-3 rounded-md">
          {error}
        </div>
      )}

      <div className="mt-6">
        <SiteHealthPanel audit={audit} />
      </div>

      <div className="mt-4">
        <GeneratePanel onRequested={refetchDrafts} />
      </div>

      {githubConnected === false && (
        <div className="mt-6 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          Connect GitHub to publish pages.{" "}
          <Link href="/integrations" className="underline font-medium">
            Go to Integrations
          </Link>{" "}
          — works with any git-deployed host (Vercel, Cloudflare Pages, Railway, Netlify).
        </div>
      )}
      {githubConnected && repos.length === 0 && (
        <div className="mt-6 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          GitHub is connected, but no repo is added yet.{" "}
          <Link href="/integrations" className="underline font-medium">
            Add the repo
          </Link>{" "}
          that hosts your site.
        </div>
      )}

      <div className="mt-6 space-y-4">
        {drafts === null ? (
          <div className="text-sm text-gray-400">loading…</div>
        ) : drafts.length === 0 ? (
          <div className="rounded-lg border border-gray-200 p-6 text-sm text-gray-500">
            Spica hasn&apos;t drafted any pages yet. Ask it to write a page for a
            target keyword and it&apos;ll show up here, ready to publish.
          </div>
        ) : (
          drafts.map((d) => <DraftCard key={d.id} draft={d} repos={repos} />)
        )}
      </div>
    </main>
  );
}
