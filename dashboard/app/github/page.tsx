"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Agent,
  Deployment,
  GitHubAgentPath,
  GitHubIntegration,
  GitHubIntegrationFresh,
  GithubConnectionState,
  GithubRepo,
  UnauthorizedError,
  addGithubRepo,
  deleteGitHubAgentPath,
  deleteGitHubIntegration,
  fetchAgents,
  fetchDeployments,
  fetchGitHubIntegration,
  fetchGithubConnection,
  handleAuthError,
  listGitHubAgentPaths,
  putGitHubAgentPath,
  putGitHubIntegration,
  removeGithubRepo,
  startGithubOAuth,
} from "../api";


// "https://github.com/owner/name" or "https://github.com/owner/name.git"
// or just "owner/name" — accept either. Returns null on anything else
// so the form can show a clean error rather than POSTing garbage.
function parseRepoInput(raw: string): { owner: string; name: string } | null {
  const trimmed = raw.trim().replace(/\.git$/, "");
  if (!trimmed) return null;
  // GitHub-style "owner/name" without protocol.
  const slashOnly = trimmed.match(/^([A-Za-z0-9._-]+)\/([A-Za-z0-9._-]+)$/);
  if (slashOnly) return { owner: slashOnly[1], name: slashOnly[2] };
  // Full URL — pull the first two path segments.
  try {
    const u = new URL(trimmed);
    if (!/github\.com$/.test(u.hostname.replace(/^www\./, ""))) return null;
    const parts = u.pathname.replace(/^\/+|\/+$/g, "").split("/");
    if (parts.length < 2) return null;
    if (!/^[A-Za-z0-9._-]+$/.test(parts[0]) || !/^[A-Za-z0-9._-]+$/.test(parts[1])) {
      return null;
    }
    return { owner: parts[0], name: parts[1] };
  } catch {
    return null;
  }
}


function fmtTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}


// ----- Connect form: shown when no integration exists ----- //


function ConnectForm({
  onConnected,
}: {
  onConnected: (fresh: GitHubIntegrationFresh) => void;
}) {
  const [repoInput, setRepoInput] = useState("");
  const [branch, setBranch] = useState("main");
  const [pat, setPat] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    const parsed = parseRepoInput(repoInput);
    if (!parsed) {
      setError(
        "Repo must be a github.com URL or owner/name (e.g. https://github.com/anthropics/claude-code or anthropics/claude-code).",
      );
      return;
    }
    if (!pat.trim()) {
      setError("Personal access token is required.");
      return;
    }
    setSubmitting(true);
    try {
      const fresh = await putGitHubIntegration({
        repo_owner: parsed.owner,
        repo_name: parsed.name,
        branch: branch.trim() || "main",
        pat: pat.trim(),
      });
      onConnected(fresh);
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={submit}
      className="rounded-lg border border-gray-200 bg-white p-5 space-y-4"
    >
      <div>
        <label className="block text-sm font-medium text-gray-900 mb-1">
          Repository
        </label>
        <input
          type="text"
          value={repoInput}
          onChange={(e) => setRepoInput(e.target.value)}
          placeholder="owner/name or https://github.com/owner/name"
          className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-500"
        />
      </div>
      <div>
        <label className="block text-sm font-medium text-gray-900 mb-1">
          Branch
        </label>
        <input
          type="text"
          value={branch}
          onChange={(e) => setBranch(e.target.value)}
          placeholder="main"
          className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-500"
        />
        <p className="mt-1 text-xs text-gray-500">
          The single branch we&apos;ll watch. Pushes to other branches are
          ignored. (Multi-branch tracking is on the Phase 10B roadmap.)
        </p>
      </div>
      <div>
        <label className="block text-sm font-medium text-gray-900 mb-1">
          Personal access token
        </label>
        <input
          type="password"
          value={pat}
          onChange={(e) => setPat(e.target.value)}
          placeholder="ghp_..."
          autoComplete="off"
          className="w-full px-3 py-2 border border-gray-300 rounded-md text-sm font-mono focus:outline-none focus:ring-2 focus:ring-accent-500"
        />
        <p className="mt-1 text-xs text-gray-500">
          To <strong>publish pages</strong>, the token needs write access: a
          classic token with the <code className="font-mono">repo</code> scope,
          or a fine-grained PAT with{" "}
          <code className="font-mono">Contents: Read and write</code> +{" "}
          <code className="font-mono">Pull requests: Read and write</code> on
          this repo. (Read-only is enough only for watching a repo, not
          publishing.) We validate the token against GitHub before storing.
          See{" "}
          <a
            href="https://github.com/settings/tokens/new"
            target="_blank"
            rel="noreferrer"
            className="text-accent-700 hover:underline"
          >
            github.com/settings/tokens/new
          </a>
          .
        </p>
      </div>
      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}
      <button
        type="submit"
        disabled={submitting}
        className="px-4 py-2 rounded-md bg-accent-600 text-white text-sm font-medium hover:bg-accent-700 disabled:opacity-50"
      >
        {submitting ? "validating..." : "connect repo"}
      </button>
    </form>
  );
}


// ----- Status block: shown after registration ----- //


function StatusBlock({
  integration,
  freshSecret,
  onDisconnect,
}: {
  integration: GitHubIntegration;
  // Populated only on first registration in this page lifecycle so
  // the user can copy the secret to their clipboard once. Refreshing
  // the page wipes it.
  freshSecret?: string;
  onDisconnect: () => void;
}) {
  const webhookUrl = integration.webhook_url;
  const [copiedField, setCopiedField] = useState<string | null>(null);

  const copy = async (field: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopiedField(field);
      setTimeout(() => setCopiedField(null), 1500);
    } catch {
      // clipboard API unavailable (insecure context, browser permission)
      // — silently no-op, the user can still copy manually.
    }
  };

  const disconnect = async () => {
    if (
      !confirm(
        "Disconnect the GitHub repo? Webhooks will stop being processed and the stored token + webhook secret will be removed. Re-registering produces a fresh secret you'll need to paste back into GitHub.",
      )
    ) {
      return;
    }
    onDisconnect();
  };

  return (
    <div className="space-y-4">
      <div className="rounded-lg border border-gray-200 bg-white p-5 space-y-3">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-xs uppercase tracking-wider text-gray-500">
              Connected repo
            </div>
            <div className="font-mono text-sm text-gray-900">
              {integration.repo_owner}/{integration.repo_name}
              <span className="mx-2 text-gray-400">·</span>
              <span className="text-gray-700">{integration.branch}</span>
            </div>
          </div>
          <button
            type="button"
            onClick={disconnect}
            className="text-sm text-red-600 hover:text-red-700 font-medium"
          >
            disconnect
          </button>
        </div>
        <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm">
          <div className="text-gray-500">token</div>
          <div className="font-mono text-gray-700">{integration.pat_masked}</div>
          <div className="text-gray-500">connected</div>
          <div className="font-mono text-gray-700">
            {fmtTime(integration.created_at)}
          </div>
          <div className="text-gray-500">last update</div>
          <div className="font-mono text-gray-700">
            {fmtTime(integration.updated_at)}
          </div>
        </div>
      </div>

      <div className="rounded-lg border border-gray-200 bg-white p-5 space-y-3">
        <div className="text-sm font-semibold text-gray-900">
          Configure GitHub&apos;s webhook
        </div>
        <p className="text-xs text-gray-600">
          On GitHub: Repo → Settings → Webhooks → Add webhook. Paste the URL
          and secret below, set <code className="font-mono">Content type</code>{" "}
          to <code className="font-mono">application/json</code>, and select{" "}
          <strong>Just the push event</strong>.
        </p>

        <div>
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-1">
            Payload URL
          </div>
          <div className="flex items-center gap-2">
            <input
              type="text"
              readOnly
              value={webhookUrl}
              className="flex-1 px-3 py-2 border border-gray-300 rounded-md text-sm font-mono bg-gray-50"
            />
            <button
              type="button"
              onClick={() => copy("url", webhookUrl)}
              className="px-3 py-2 text-sm border border-gray-300 rounded-md text-gray-700 hover:bg-gray-50"
            >
              {copiedField === "url" ? "copied" : "copy"}
            </button>
          </div>
        </div>

        <div>
          <div className="text-xs uppercase tracking-wider text-gray-500 mb-1">
            Secret
          </div>
          {freshSecret ? (
            <div className="space-y-2">
              <div className="flex items-center gap-2">
                <input
                  type="text"
                  readOnly
                  value={freshSecret}
                  className="flex-1 px-3 py-2 border border-amber-300 rounded-md text-sm font-mono bg-amber-50 text-amber-900"
                />
                <button
                  type="button"
                  onClick={() => copy("secret", freshSecret)}
                  className="px-3 py-2 text-sm border border-amber-300 rounded-md text-amber-900 bg-amber-50 hover:bg-amber-100 font-medium"
                >
                  {copiedField === "secret" ? "copied" : "copy"}
                </button>
              </div>
              <p className="text-xs text-amber-700">
                Save this secret now — it&apos;s shown once and stored
                encrypted afterwards. To rotate, disconnect + re-register.
              </p>
            </div>
          ) : (
            <div className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-600">
              Stored encrypted. To rotate, disconnect + re-register — that
              produces a fresh secret you&apos;ll paste into GitHub.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}


// ----- Agent-path mapping table ----- //


function AgentPathsBlock({
  paths,
  agents,
  onChanged,
}: {
  paths: GitHubAgentPath[];
  agents: Agent[];
  onChanged: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const [agentName, setAgentName] = useState("");
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const mappedAgents = useMemo(
    () => new Set(paths.map((p) => p.agent_name)),
    [paths],
  );
  const availableAgents = useMemo(
    () => agents.filter((a) => !mappedAgents.has(a.name)),
    [agents, mappedAgents],
  );

  const reset = () => {
    setAgentName("");
    setPath("");
    setError(null);
  };

  const startAdd = () => {
    setAdding(true);
    if (availableAgents.length > 0) {
      setAgentName(availableAgents[0].name);
    }
  };

  const cancel = () => {
    setAdding(false);
    reset();
  };

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!agentName) {
      setError("Pick an agent to map.");
      return;
    }
    if (!path.trim()) {
      setError("Path is required.");
      return;
    }
    setSubmitting(true);
    try {
      await putGitHubAgentPath(agentName, path.trim());
      onChanged();
      cancel();
    } catch (e) {
      setError(String(e));
    } finally {
      setSubmitting(false);
    }
  };

  const remove = async (name: string) => {
    if (!confirm(`Remove the path mapping for "${name}"?`)) return;
    try {
      await deleteGitHubAgentPath(name);
      onChanged();
    } catch (e) {
      alert(String(e));
    }
  };

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <div className="text-sm font-semibold text-gray-900">
            Agent paths
          </div>
          <p className="text-xs text-gray-600">
            Map each agent to its directory in the repo. A push that touches
            files under that path will redeploy the agent automatically.
          </p>
        </div>
        {!adding && (
          <button
            type="button"
            onClick={startAdd}
            disabled={availableAgents.length === 0}
            className="px-3 py-1.5 text-sm border border-gray-300 rounded-md text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            title={
              availableAgents.length === 0
                ? "Every registered agent already has a path mapping"
                : ""
            }
          >
            add path
          </button>
        )}
      </div>

      {paths.length === 0 && !adding && (
        <div className="text-sm text-gray-500 italic">
          No paths mapped yet. Add one above to enable push-to-deploy.
        </div>
      )}

      {paths.length > 0 && (
        <div className="border border-gray-200 rounded-md divide-y divide-gray-100">
          {paths.map((p) => (
            <div
              key={p.agent_name}
              className="px-3 py-2 flex items-center justify-between text-sm"
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className="font-mono text-gray-900">{p.agent_name}</span>
                <span className="text-gray-300">→</span>
                <span className="font-mono text-gray-700 truncate">
                  {p.path}
                </span>
              </div>
              <button
                type="button"
                onClick={() => remove(p.agent_name)}
                className="text-xs text-red-600 hover:text-red-700 font-medium"
              >
                remove
              </button>
            </div>
          ))}
        </div>
      )}

      {adding && (
        <form
          onSubmit={save}
          className="border border-gray-200 rounded-md p-3 space-y-3 bg-gray-50"
        >
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-700 mb-1">
                Agent
              </label>
              <select
                value={agentName}
                onChange={(e) => setAgentName(e.target.value)}
                className="w-full px-2 py-1.5 border border-gray-300 rounded-md text-sm font-mono bg-white"
              >
                {availableAgents.length === 0 && (
                  <option value="">no available assistants</option>
                )}
                {availableAgents.map((a) => (
                  <option key={a.name} value={a.name}>
                    {a.name}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-700 mb-1">
                Path in repo
              </label>
              <input
                type="text"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                placeholder="polaris  or  bots/scout"
                className="w-full px-2 py-1.5 border border-gray-300 rounded-md text-sm font-mono bg-white"
              />
            </div>
          </div>
          <p className="text-xs text-gray-500">
            Repo-relative. No leading slash, no <code>..</code>. Forward
            slashes for nested dirs.
          </p>
          {error && (
            <div className="text-xs text-red-700">{error}</div>
          )}
          <div className="flex items-center gap-2">
            <button
              type="submit"
              disabled={submitting || availableAgents.length === 0}
              className="px-3 py-1.5 text-sm bg-accent-600 text-white rounded-md hover:bg-accent-700 disabled:opacity-50"
            >
              {submitting ? "saving..." : "save"}
            </button>
            <button
              type="button"
              onClick={cancel}
              className="px-3 py-1.5 text-sm border border-gray-300 rounded-md text-gray-700 hover:bg-gray-50"
            >
              cancel
            </button>
          </div>
        </form>
      )}
    </div>
  );
}


// ----- Recent github-triggered deploys ----- //


function RecentDeploysBlock({ deploys }: { deploys: Deployment[] }) {
  const githubDeploys = deploys.filter((d) => d.source === "github_push");
  if (githubDeploys.length === 0) {
    return (
      <div className="rounded-lg border border-gray-200 bg-white p-5">
        <div className="text-sm font-semibold text-gray-900 mb-1">
          Recent push-triggered deploys
        </div>
        <p className="text-sm text-gray-500 italic">
          None yet. Push to a registered branch + path to see deploys land here.
        </p>
      </div>
    );
  }
  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 space-y-3">
      <div className="text-sm font-semibold text-gray-900">
        Recent push-triggered deploys
      </div>
      <div className="border border-gray-200 rounded-md divide-y divide-gray-100">
        {githubDeploys.slice(0, 10).map((d) => (
          <div
            key={d.id}
            className="px-3 py-2 flex items-center justify-between text-sm"
          >
            <div className="flex items-center gap-3 min-w-0">
              <span
                className={
                  "inline-block px-2 py-0.5 rounded-full text-[10px] font-medium uppercase tracking-wider " +
                  (d.status === "running"
                    ? "bg-green-100 text-green-800"
                    : d.status === "failed"
                    ? "bg-red-100 text-red-800"
                    : d.status === "stopped"
                    ? "bg-gray-100 text-gray-700"
                    : d.status === "building"
                    ? "bg-amber-100 text-amber-800"
                    : "bg-blue-100 text-blue-800")
                }
              >
                {d.status}
              </span>
              <Link
                href={`/deployments/${d.id}`}
                className="font-mono text-gray-700 hover:text-accent-700 truncate"
              >
                {d.id.slice(0, 8)}…
              </Link>
              <span className="font-mono text-gray-500">{d.agent_name}</span>
              {d.source_commit_sha && (
                <span
                  className="font-mono text-gray-400 text-xs"
                  title={d.source_commit_sha}
                >
                  {d.source_commit_sha.slice(0, 7)}
                </span>
              )}
            </div>
            <span className="text-xs text-gray-400 font-mono">
              {fmtTime(d.created_at)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}


// ----- Page ----- //


// Phase 10B.5: OAuth connect + multi-repo. Self-contained: fetches its
// own connection state so it can sit above the legacy PAT flow.
function GithubOAuthBlock() {
  const router = useRouter();
  const [state, setState] = useState<GithubConnectionState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [owner, setOwner] = useState("");
  const [name, setName] = useState("");
  const [branch, setBranch] = useState("main");
  const [adding, setAdding] = useState(false);
  const [newSecret, setNewSecret] = useState<{ repo: string; secret: string } | null>(null);

  const load = useCallback(async () => {
    try {
      setState(await fetchGithubConnection());
      setError(null);
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    load();
  }, [load]);

  const connect = async () => {
    try {
      const { authorization_url } = await startGithubOAuth(
        typeof window !== "undefined" ? window.location.href : undefined,
      );
      window.location.href = authorization_url;
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    }
  };

  const add = async () => {
    if (!owner.trim() || !name.trim()) return;
    setAdding(true);
    setError(null);
    try {
      const repo = await addGithubRepo({
        repo_owner: owner.trim(),
        repo_name: name.trim(),
        branch: branch.trim() || "main",
      });
      if (repo.webhook_secret) {
        setNewSecret({ repo: `${repo.repo_owner}/${repo.repo_name}`, secret: repo.webhook_secret });
      }
      setOwner("");
      setName("");
      await load();
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    } finally {
      setAdding(false);
    }
  };

  const remove = async (repo: GithubRepo) => {
    try {
      await removeGithubRepo(repo.id);
      await load();
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    }
  };

  if (loading) return <div className="text-sm text-gray-500">loading GitHub connection…</div>;

  const conn = state?.connection ?? null;

  return (
    <div className="rounded-lg border border-gray-200 bg-white p-5 space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-gray-900">GitHub connection (OAuth)</h2>
        {conn ? (
          <span className="text-xs text-gray-500">
            connected
            {conn.github_login ? <> as <span className="font-mono text-gray-700">@{conn.github_login}</span></> : null}
            {" "}· {conn.auth_kind}
          </span>
        ) : null}
      </div>

      {error && (
        <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>
      )}

      {!conn ? (
        <div className="space-y-3">
          <p className="text-sm text-gray-600">
            Connect your GitHub account once, then watch multiple repos. The
            classic PAT flow below still works as a fallback.
          </p>
          <button
            onClick={connect}
            className="px-4 py-2 text-sm font-medium rounded-md bg-gray-900 text-white hover:bg-gray-800"
          >
            Connect GitHub
          </button>
        </div>
      ) : (
        <div className="space-y-4">
          {conn.auth_kind !== "oauth" ? (
            // A pasted token (auth_kind 'pat') is usually read-only, which
            // makes publishing fail with a 403 at the worst moment. Always
            // offer the OAuth upgrade: the callback upserts this same
            // connection, flipping it to a write-capable oauth token.
            <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-3 text-sm text-amber-900 space-y-2">
              <div>
                This connection uses a pasted token. Pasted tokens are often
                read-only, so <strong>publishing pages can fail</strong>.
                Switch to OAuth to grant write access with one click. No token
                to manage.
              </div>
              <button
                onClick={connect}
                className="px-4 py-2 text-sm font-medium rounded-md bg-gray-900 text-white hover:bg-gray-800"
              >
                Switch to OAuth
              </button>
            </div>
          ) : (
            <div className="flex items-center justify-between">
              <p className="text-sm text-gray-600">
                Connected via OAuth (write access for publishing).
              </p>
              <button
                onClick={connect}
                className="text-xs text-gray-500 hover:text-gray-700 underline"
              >
                Reconnect
              </button>
            </div>
          )}
          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wide text-gray-500 mb-2">Watched repos</h3>
            {state && state.repos.length > 0 ? (
              <div className="border border-gray-200 rounded-md divide-y divide-gray-100">
                {state.repos.map((r) => (
                  <div key={r.id} className="flex items-center justify-between px-3 py-2 text-sm">
                    <span className="font-mono text-gray-800">
                      {r.repo_owner}/{r.repo_name}
                      <span className="text-gray-400"> · {r.branch}</span>
                      {!r.is_active && <span className="ml-2 text-amber-600">(inactive)</span>}
                    </span>
                    <button onClick={() => remove(r)} className="text-xs text-red-600 hover:text-red-800">
                      remove
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-sm text-gray-500">No repos yet. Add one below.</p>
            )}
          </div>

          <div className="flex flex-wrap items-end gap-2">
            <input
              value={owner}
              onChange={(e) => setOwner(e.target.value)}
              placeholder="owner"
              className="px-3 py-2 border border-gray-300 rounded-md text-sm font-mono w-32"
            />
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="repo"
              className="px-3 py-2 border border-gray-300 rounded-md text-sm font-mono w-40"
            />
            <input
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              placeholder="branch"
              className="px-3 py-2 border border-gray-300 rounded-md text-sm font-mono w-28"
            />
            <button
              onClick={add}
              disabled={adding || !owner.trim() || !name.trim()}
              className="px-3 py-2 text-sm border border-gray-300 rounded-md text-gray-700 hover:bg-gray-50 disabled:opacity-50"
            >
              {adding ? "adding…" : "Add repo"}
            </button>
          </div>

          {newSecret && (
            <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900 space-y-1">
              <div>
                Webhook secret for <span className="font-mono">{newSecret.repo}</span> (shown once — paste it
                into the repo&apos;s webhook settings):
              </div>
              <div className="font-mono break-all">{newSecret.secret}</div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


export default function GitHubPage() {
  const router = useRouter();
  const [integration, setIntegration] = useState<GitHubIntegration | null>(null);
  const [freshSecret, setFreshSecret] = useState<string | undefined>(undefined);
  const [paths, setPaths] = useState<GitHubAgentPath[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [deploys, setDeploys] = useState<Deployment[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const loadAll = useCallback(async () => {
    setError(null);
    try {
      // Run in parallel where possible. Paths + deploys are only
      // meaningful when an integration exists, but they cost the same
      // as a no-op so we don't gate on integration presence first.
      const [integ, agentList, deployList] = await Promise.all([
        fetchGitHubIntegration(),
        fetchAgents().catch(() => [] as Agent[]),
        fetchDeployments().catch(() => [] as Deployment[]),
      ]);
      setIntegration(integ);
      setAgents(agentList);
      setDeploys(deployList);
      if (integ) {
        const ps = await listGitHubAgentPaths().catch(() => [] as GitHubAgentPath[]);
        setPaths(ps);
      } else {
        setPaths([]);
      }
    } catch (e) {
      if (handleAuthError(e, router)) return;
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [router]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const onConnected = (fresh: GitHubIntegrationFresh) => {
    setIntegration(fresh);
    setFreshSecret(fresh.webhook_secret);
    // Reload paths + agents so the path-mapping UI shows up immediately.
    loadAll();
  };

  const onDisconnect = async () => {
    try {
      await deleteGitHubIntegration();
      setIntegration(null);
      setFreshSecret(undefined);
      setPaths([]);
    } catch (e) {
      alert(String(e));
    }
  };

  return (
    <main className="max-w-3xl mx-auto px-4 sm:px-6 py-6">

      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-gray-900">GitHub</h1>
        <p className="mt-1 text-sm text-gray-600">
          Connect a GitHub repo to push-to-deploy bots and let Polaris read
          docs straight from the repo. Connect once via OAuth and watch
          multiple repos, or use the classic PAT flow below.
        </p>
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
          {error}
        </div>
      )}

      <div className="mb-6">
        <GithubOAuthBlock />
      </div>

      {loading ? (
        <div className="text-sm text-gray-500">loading…</div>
      ) : !integration ? (
        <ConnectForm onConnected={onConnected} />
      ) : (
        <div className="space-y-6">
          <StatusBlock
            integration={integration}
            freshSecret={freshSecret}
            onDisconnect={onDisconnect}
          />
          <AgentPathsBlock
            paths={paths}
            agents={agents}
            onChanged={loadAll}
          />
          <RecentDeploysBlock deploys={deploys} />
        </div>
      )}
    </main>
  );
}
