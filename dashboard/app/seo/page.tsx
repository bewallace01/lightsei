"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  GithubRepo,
  SeoDraft,
  fetchGithubConnection,
  fetchSeoDrafts,
  handleAuthError,
  publishPage,
} from "../api";

/** A complete, deployable HTML page built from Spica's draft fields. The
 * owner can edit the path/format for their framework before publishing. */
function pageHtml(d: SeoDraft): string {
  const p = d.page;
  const esc = (s: string) =>
    (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  return [
    "<!doctype html>",
    '<html lang="en">',
    "<head>",
    '<meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    `<title>${esc(p.title)}</title>`,
    `<meta name="description" content="${esc(p.meta_description)}">`,
    "</head>",
    "<body>",
    `<h1>${esc(p.h1)}</h1>`,
    p.body_html || "",
    "</body>",
    "</html>",
    "",
  ].join("\n");
}

type PublishState = {
  path: string;
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
  const defaultPath = `lightsei-pages/${draft.page.slug || "page"}.html`;
  const [st, setSt] = useState<PublishState>({
    path: defaultPath,
    repoId: repos[0]?.id ?? "",
    busy: false,
  });
  const [open, setOpen] = useState(false);

  async function onPublish() {
    if (!st.repoId) return;
    setSt((s) => ({ ...s, busy: true, error: undefined, result: undefined }));
    try {
      const res = await publishPage({
        repo_id: st.repoId,
        path: st.path.trim(),
        content: pageHtml(draft),
        title: draft.page.title || draft.page.h1 || "New page",
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
            File path in repo
            <input
              value={st.path}
              onChange={(e) => setSt((s) => ({ ...s, path: e.target.value }))}
              className="mt-1 w-full text-sm font-mono rounded-md ring-1 ring-gray-300 px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent-600"
            />
            <span className="text-[11px] text-gray-400">
              Where the page file goes. Adjust for your framework (e.g. a
              markdown file under content/, or an HTML file in your public dir).
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
  const [githubConnected, setGithubConnected] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [d, gh] = await Promise.all([fetchSeoDrafts(), fetchGithubConnection()]);
        if (!alive) return;
        setDrafts(d);
        setRepos(gh.repos.filter((r) => r.is_active));
        setGithubConnected(gh.connection !== null);
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
