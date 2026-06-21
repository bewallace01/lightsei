"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import {
  GithubRepo,
  PageFormat,
  SeoAuditState,
  SeoCrawl,
  SeoDraft,
  SeoSuggestion,
  designFormat,
  fetchDesignResult,
  fetchRepoPageFiles,
  fetchGithubConnection,
  fetchSeoAudit,
  fetchSeoCrawl,
  fetchSeoDrafts,
  fetchSeoSuggestions,
  generateSeoPage,
  handleAuthError,
  publishPage,
  runSeoAudit,
  runSeoCrawl,
  runSeoSuggestions,
} from "../api";

/** "Page ideas" — Spica suggests new pages worth creating, each with a
 * one-click "Draft this" that hands the keyword to the generator. */
function IdeasPanel({
  initial,
  onDraft,
}: {
  initial: SeoSuggestion[];
  onDraft: (keyword: string, pageType: string) => void;
}) {
  const [ideas, setIdeas] = useState<SeoSuggestion[]>(initial);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => setIdeas(initial), [initial]);

  async function refresh() {
    try {
      setIdeas(await fetchSeoSuggestions());
    } catch {
      /* best-effort */
    }
  }

  async function onGetIdeas() {
    setBusy(true);
    setNote(null);
    try {
      await runSeoSuggestions();
      setNote("Spica is thinking up page ideas. They appear in a moment.");
      setTimeout(refresh, 8000);
      setTimeout(refresh, 18000);
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-gray-200 p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-medium text-gray-900">Page ideas</div>
          <div className="text-xs text-gray-500 mt-0.5">
            Pages worth creating to win more search traffic.
          </div>
        </div>
        <button
          onClick={onGetIdeas}
          disabled={busy}
          className="text-xs px-2.5 py-1 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          {busy ? "Thinking…" : "Get ideas"}
        </button>
      </div>
      {note && <p className="mt-2 text-xs text-gray-500">{note}</p>}
      {ideas.length > 0 && (
        <ul className="mt-3 border-t border-gray-100 pt-3 space-y-2">
          {ideas.map((idea, i) => (
            <li key={i} className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-sm text-gray-900">
                  {idea.keyword}{" "}
                  <span className="text-[10px] uppercase tracking-wider text-gray-400">
                    {idea.page_type}
                  </span>
                </div>
                {idea.rationale && (
                  <div className="text-xs text-gray-500">{idea.rationale}</div>
                )}
              </div>
              <button
                onClick={() => onDraft(idea.keyword, idea.page_type)}
                className="shrink-0 text-xs px-2.5 py-1 rounded-md bg-accent-600 text-white hover:bg-accent-700"
              >
                Draft this
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** "Whole-site crawl" — audits the homepage + the pages it links to, with a
 * per-page score table and a rollup of the most common issues. */
function CrawlPanel({ initial }: { initial: SeoCrawl | null }) {
  const [crawl, setCrawl] = useState<SeoCrawl | null>(initial);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  useEffect(() => setCrawl(initial), [initial]);

  async function refresh() {
    try {
      setCrawl((await fetchSeoCrawl()).latest);
    } catch {
      /* best-effort */
    }
  }

  async function onCrawl() {
    setBusy(true);
    setNote(null);
    try {
      await runSeoCrawl();
      setNote("Spica is crawling your site. Results appear in a moment.");
      setTimeout(refresh, 10000);
      setTimeout(refresh, 22000);
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  }

  const scoreColor = (s: number) =>
    s >= 85 ? "text-emerald-600" : s >= 60 ? "text-amber-600" : "text-red-600";

  return (
    <div className="rounded-lg border border-gray-200 p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <div className="text-sm font-medium text-gray-900">Whole-site crawl</div>
          <div className="text-xs text-gray-500 mt-0.5">
            {crawl
              ? `${crawl.pages_audited} page(s) · avg ${crawl.average_score}/100 · lowest ${crawl.lowest_score}`
              : "Audit your homepage and the pages it links to."}
          </div>
        </div>
        <button
          onClick={onCrawl}
          disabled={busy}
          className="text-xs px-2.5 py-1 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
        >
          {busy ? "Crawling…" : "Audit whole site"}
        </button>
      </div>
      {note && <p className="mt-2 text-xs text-gray-500">{note}</p>}
      {crawl && crawl.pages.length > 0 && (
        <div className="mt-3 border-t border-gray-100 pt-3">
          <ul className="space-y-1">
            {crawl.pages.map((pg, i) => (
              <li key={i} className="flex items-center justify-between text-sm gap-3">
                <span className="font-mono text-gray-600 truncate">{pg.url}</span>
                {pg.reachable ? (
                  <span className={"font-medium shrink-0 " + scoreColor(pg.score)}>{pg.score}</span>
                ) : (
                  <span className="text-xs text-red-500 shrink-0">unreachable</span>
                )}
              </li>
            ))}
          </ul>
          {crawl.top_findings.length > 0 && (
            <p className="mt-3 text-xs text-gray-500">
              Most common:{" "}
              {crawl.top_findings
                .map((f) => `${f.check.replace(/_/g, " ")} (${f.pages})`)
                .join(", ")}
            </p>
          )}
        </div>
      )}
    </div>
  );
}

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

/** A plain full-HTML document from the draft's fields — the input Capella
 * restyles when polishing. */
function basicPageHtml(d: SeoDraft): string {
  const p = d.page;
  const esc = (s: string) =>
    (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  return [
    "<!doctype html>",
    '<html lang="en"><head><meta charset="utf-8">',
    '<meta name="viewport" content="width=device-width, initial-scale=1">',
    `<title>${esc(p.title)}</title>`,
    `<meta name="description" content="${esc(p.meta_description)}">`,
    "</head><body>",
    `<h1>${esc(p.h1)}</h1>`,
    p.body_html || "",
    "</body></html>",
  ].join("\n");
}

/** Open a full HTML page in a new browser tab so the owner sees exactly what
 * will publish (the styled version once polished). Uses a blob URL so the
 * complete document — including Capella's embedded CSS — renders for real. */
function openPreview(html: string) {
  const blob = new Blob([html], { type: "text/html" });
  const url = URL.createObjectURL(blob);
  window.open(url, "_blank", "noopener");
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

function DraftCard({
  draft,
  repos,
  matchSite,
}: {
  draft: SeoDraft;
  repos: GithubRepo[];
  matchSite?: string;
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
  // Capella's output (styled HTML, or a matching page component), when produced.
  const [polished, setPolished] = useState<string | null>(null);
  const [isComponent, setIsComponent] = useState(false);
  const [polishBusy, setPolishBusy] = useState(false);
  const [polishNote, setPolishNote] = useState<string | null>(null);
  // A live page on the owner's site for Capella to match the look of.
  const [matchUrl, setMatchUrl] = useState(matchSite ?? "");
  // Component mode: pick an existing page in the repo as a template.
  const [templateFiles, setTemplateFiles] = useState<string[] | null>(null);
  const [templatePath, setTemplatePath] = useState("");

  function onFormat(format: PageFormat) {
    const def = FORMATS.find((f) => f.value === format)!.path(slug);
    // Re-default the path when the owner hasn't hand-edited it.
    setSt((s) => ({ ...s, format, path: s.pathEdited ? s.path : def }));
  }

  // Poll Capella for a result; returns the output text or throws.
  async function pollResult(commandId: string): Promise<string> {
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 2500));
      const res = await fetchDesignResult(commandId);
      if (res.status === "formatted" && res.output) return res.output;
      if (res.status === "failed") throw new Error(res.error || "Capella failed");
    }
    throw new Error("still working — check back in a moment");
  }

  async function loadTemplates() {
    if (!st.repoId || templateFiles) return;
    try {
      setTemplateFiles(await fetchRepoPageFiles(st.repoId));
    } catch {
      setTemplateFiles([]);
    }
  }

  async function onPolish() {
    setPolishBusy(true);
    setPolishNote(null);
    try {
      const { command_id, design_assistant_deployed } = await designFormat({
        content: basicPageHtml(draft),
        content_type: "page",
        ...(matchUrl.trim() ? { match_url: matchUrl.trim() } : {}),
      });
      if (!design_assistant_deployed) {
        setPolishNote("Add the Design assistant (Capella) to your team to polish pages.");
        setPolishBusy(false);
        return;
      }
      const output = await pollResult(command_id);
      setPolished(output);
      setIsComponent(false);
      setSt((s) => ({
        ...s,
        format: "html",
        path: s.pathEdited ? s.path : FORMATS[0].path(slug),
      }));
      setPolishNote(
        matchUrl.trim()
          ? `Polished to match ${matchUrl.trim()}. Publishing uses the styled version.`
          : "Polished. Publishing will use the styled version.",
      );
    } catch (e) {
      setPolishNote(String(e));
    } finally {
      setPolishBusy(false);
    }
  }

  // Generate a new page COMPONENT that matches an existing page in the repo.
  async function onGenerateComponent() {
    if (!st.repoId || !templatePath) return;
    setPolishBusy(true);
    setPolishNote(null);
    try {
      const { command_id, design_assistant_deployed } = await designFormat({
        content: basicPageHtml(draft),
        content_type: "component",
        template_repo_id: st.repoId,
        template_path: templatePath,
      });
      if (!design_assistant_deployed) {
        setPolishNote("Add the Design assistant (Capella) to your team first.");
        setPolishBusy(false);
        return;
      }
      const output = await pollResult(command_id);
      setPolished(output);
      setIsComponent(true);
      // Publish the component next to the template it matched, with a name
      // derived from the page slug.
      const dir = templatePath.includes("/")
        ? templatePath.slice(0, templatePath.lastIndexOf("/") + 1)
        : "";
      const ext = templatePath.slice(templatePath.lastIndexOf("."));
      const name = slug
        .split("-")
        .map((p) => p.charAt(0).toUpperCase() + p.slice(1))
        .join("");
      setSt((s) => ({ ...s, pathEdited: true, path: `${dir}${name}Page${ext}` }));
      setPolishNote(
        `Generated a page matching ${templatePath}. Review it, then publish (you may need to add it to your router).`,
      );
    } catch (e) {
      setPolishNote(String(e));
    } finally {
      setPolishBusy(false);
    }
  }

  async function onPublish() {
    if (!st.repoId) return;
    setSt((s) => ({ ...s, busy: true, error: undefined, result: undefined }));
    try {
      const res = await publishPage({
        repo_id: st.repoId,
        title: draft.page.title || draft.page.h1 || "New page",
        // Use Capella's styled HTML if polished; otherwise the backend
        // renders the chosen format from the structured page.
        ...(polished
          ? { content: polished, path: st.path.trim() || FORMATS[0].path(slug) }
          : {
              page: draft.page,
              format: st.format,
              ...(st.pathEdited ? { path: st.path.trim() } : {}),
            }),
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

      {polished && (
        <div className="mt-3 text-xs text-emerald-700 flex items-center gap-1">
          {isComponent
            ? "🧩 Generated a page component matching your repo — publishing commits the code."
            : "🎨 Polished by Capella — publishing uses the styled version."}
        </div>
      )}
      {!open && (
        <label className="mt-3 block text-xs text-gray-500">
          Match my site&apos;s design (optional)
          <input
            type="url"
            value={matchUrl}
            onChange={(e) => setMatchUrl(e.target.value)}
            placeholder="https://yoursite.com"
            className="mt-1 w-full text-sm rounded-md ring-1 ring-gray-300 px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent-600"
          />
          <span className="text-[11px] text-gray-400">
            Capella reads this page&apos;s fonts &amp; colors so the new page matches your site.
          </span>
        </label>
      )}
      {!open && repos.length > 0 && (
        <div className="mt-3 rounded-md border border-gray-200 bg-gray-50 p-3">
          <div className="text-xs font-medium text-gray-700">
            Match a page in my codebase (best for React / Vite / Next sites)
          </div>
          <div className="text-[11px] text-gray-500 mb-2">
            Pick one of your existing pages; Capella writes a new page that uses
            the same layout, components, and styling.
          </div>
          <div className="flex flex-col sm:flex-row gap-2">
            <select
              value={templatePath}
              onFocus={loadTemplates}
              onChange={(e) => setTemplatePath(e.target.value)}
              className="flex-1 text-xs font-mono rounded-md ring-1 ring-gray-300 px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent-600"
            >
              <option value="">
                {templateFiles === null
                  ? "Pick a page to match…"
                  : templateFiles.length === 0
                  ? "No page files found in this repo"
                  : "Pick a page to match…"}
              </option>
              {(templateFiles ?? []).map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
            <button
              onClick={onGenerateComponent}
              disabled={polishBusy || !templatePath}
              className="text-xs px-3 py-1.5 rounded-md bg-accent-600 text-white hover:bg-accent-700 disabled:opacity-50"
            >
              {polishBusy ? "Generating…" : "Generate matching page"}
            </button>
          </div>
        </div>
      )}
      {!open ? (
        <div className="mt-4 flex items-center gap-2 flex-wrap">
          <button
            onClick={() => {
              if (isComponent && polished) {
                // A component is code, not a renderable page — show the source.
                const blob = new Blob([polished], { type: "text/plain" });
                const url = URL.createObjectURL(blob);
                window.open(url, "_blank", "noopener");
                setTimeout(() => URL.revokeObjectURL(url), 60000);
              } else {
                openPreview(polished ?? basicPageHtml(draft));
              }
            }}
            className="text-sm px-3 py-1.5 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50"
            title="Open the page (or the generated code) in a new tab"
          >
            {isComponent ? "👁 View code" : `👁 Preview${polished ? " (styled)" : ""}`}
          </button>
          <button
            onClick={onPolish}
            disabled={polishBusy}
            className="text-sm px-3 py-1.5 rounded-md border border-gray-300 text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            title="Have Capella restyle this page so it looks good"
          >
            {polishBusy ? "Polishing…" : polished ? "Re-polish" : "🎨 Polish design"}
          </button>
          <button
            onClick={() => setOpen(true)}
            className="text-sm px-3 py-1.5 rounded-md bg-accent-600 text-white hover:bg-accent-700"
          >
            Publish to my site →
          </button>
          {polishNote && <span className="text-xs text-gray-500">{polishNote}</span>}
        </div>
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
  const [crawl, setCrawl] = useState<SeoCrawl | null>(null);
  const [ideas, setIdeas] = useState<SeoSuggestion[]>([]);
  const [githubConnected, setGithubConnected] = useState<boolean | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refetchDrafts() {
    try {
      setDrafts(await fetchSeoDrafts());
    } catch {
      /* best-effort refresh */
    }
  }

  async function draftFromIdea(keyword: string, pageType: string) {
    try {
      await generateSeoPage({ keyword, page_type: pageType });
      setTimeout(refetchDrafts, 6000);
      setTimeout(refetchDrafts, 15000);
    } catch {
      /* surfaced on next refetch */
    }
  }

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const [d, gh, a, c, ix] = await Promise.all([
          fetchSeoDrafts(),
          fetchGithubConnection(),
          fetchSeoAudit().catch(() => null),
          fetchSeoCrawl().then((r) => r.latest).catch(() => null),
          fetchSeoSuggestions().catch(() => []),
        ]);
        if (!alive) return;
        setDrafts(d);
        setRepos(gh.repos.filter((r) => r.is_active));
        setGithubConnected(gh.connection !== null);
        setAudit(a);
        setCrawl(c);
        setIdeas(ix);
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
        <CrawlPanel initial={crawl} />
      </div>

      <div className="mt-4">
        <IdeasPanel initial={ideas} onDraft={draftFromIdea} />
      </div>

      <div className="mt-4">
        <GeneratePanel onRequested={refetchDrafts} />
      </div>

      {githubConnected === false && (
        <div className="mt-6 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          Connect GitHub to publish pages.{" "}
          <Link href="/github" className="underline font-medium">
            Connect GitHub →
          </Link>{" "}
          — works with any git-deployed host (Vercel, Cloudflare Pages, Railway, Netlify).
        </div>
      )}
      {githubConnected && repos.length === 0 && (
        <div className="mt-6 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          GitHub is connected, but no repo is added yet.{" "}
          <Link href="/github" className="underline font-medium">
            Add your site&apos;s repo →
          </Link>{" "}
          so Spica can open pull requests there.
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
          drafts.map((d) => (
            <DraftCard
              key={d.id}
              draft={d}
              repos={repos}
              matchSite={audit?.configured_url ?? undefined}
            />
          ))
        )}
      </div>
    </main>
  );
}
