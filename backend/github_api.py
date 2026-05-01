"""Thin GitHub REST API client for Lightsei's Phase 10 integration.

We don't use the official `PyGithub` client — we only need three
things across the whole phase:

  - validate_pat(owner, name, pat) → ping GET /repos/{owner}/{name}
    on PUT /workspaces/me/github so wrong tokens fail at registration
    time instead of at first webhook.
  - fetch_file_content(...) → 10.4: Polaris reads MEMORY.md / TASKS.md
    from a repo path on every tick.
  - fetch_directory_tree(...) → 10.3: build a deploy zip from a
    pushed commit's view of an agent's bot dir.

A 200-line module is cheaper and easier to test than a wrapped SDK.

Tests mock httpx.Client at the module level via `github_api.httpx`,
following the same pattern Phase 9.2's notifications tests use.
"""
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

logger = logging.getLogger("lightsei.github")

GITHUB_API_BASE = "https://api.github.com"

# Interactive endpoints (PAT validation on PUT) get a tighter timeout
# than background fetches — we don't want a registration request to
# hang for 30s if GitHub is slow. Phase 10.3/10.4 background fetches
# can use a longer timeout.
INTERACTIVE_TIMEOUT_S = 5.0
BACKGROUND_TIMEOUT_S = 15.0


class GitHubAPIError(Exception):
    """Raised when GitHub returns a non-2xx response we can't recover
    from. Endpoint code translates this into HTTPException(400 or 502)
    depending on whether the cause is the user's bad input (token,
    repo) or a transient API failure."""

    def __init__(
        self,
        message: str,
        *,
        status: Optional[int] = None,
        kind: str = "github_api_error",
    ):
        super().__init__(message)
        self.message = message
        self.status = status
        self.kind = kind  # 'auth' | 'not_found' | 'transport' | 'github_api_error'


@dataclass
class RepoMetadata:
    full_name: str          # 'owner/name'
    default_branch: str
    private: bool


def validate_pat(*, repo_owner: str, repo_name: str, pat: str) -> RepoMetadata:
    """Ping `GET /repos/{owner}/{name}` with the PAT. On success returns
    metadata the caller can echo back to the user (default branch is
    a useful hint). On auth failure (401), repo-not-found-or-no-access
    (404), or scope failure (403) raises GitHubAPIError with `kind=auth`
    or `kind=not_found`. Transient failures raise `kind=transport`.

    GitHub returns 404 for both "repo doesn't exist" and "repo exists
    but this token can't see it" — by design, to avoid leaking
    private-repo existence. We don't try to disambiguate; the message
    just says "couldn't reach the repo with this token."
    """
    url = f"{GITHUB_API_BASE}/repos/{repo_owner}/{repo_name}"
    headers = {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lightsei-backend",
    }
    try:
        with httpx.Client(timeout=INTERACTIVE_TIMEOUT_S) as client:
            r = client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise GitHubAPIError(
            f"GitHub did not respond within {INTERACTIVE_TIMEOUT_S}s",
            kind="transport",
        ) from exc
    except httpx.HTTPError as exc:
        raise GitHubAPIError(
            f"network error reaching GitHub: {type(exc).__name__}",
            kind="transport",
        ) from exc

    if r.status_code == 401:
        raise GitHubAPIError(
            "GitHub rejected the personal access token (401). "
            "Generate a new fine-grained PAT with 'Contents: read' on this repo.",
            status=401,
            kind="auth",
        )
    if r.status_code == 403:
        # 403 typically means scope missing or rate-limited. Surface
        # the message so the user can debug.
        raise GitHubAPIError(
            "GitHub returned 403 — the PAT exists but lacks the required scope, "
            "or you've hit a rate limit. Make sure the token grants "
            "'Contents: read' on the target repo.",
            status=403,
            kind="auth",
        )
    if r.status_code == 404:
        raise GitHubAPIError(
            f"couldn't find repo {repo_owner}/{repo_name} with this token. "
            "Either the repo doesn't exist, or the PAT can't see it. "
            "Verify the owner/name spelling and the token's repo access.",
            status=404,
            kind="not_found",
        )
    if not (200 <= r.status_code < 300):
        raise GitHubAPIError(
            f"GitHub returned {r.status_code}: {(r.text or '')[:200]}",
            status=r.status_code,
            kind="github_api_error",
        )

    data = r.json()
    return RepoMetadata(
        full_name=data.get("full_name", f"{repo_owner}/{repo_name}"),
        default_branch=data.get("default_branch", "main"),
        private=bool(data.get("private", False)),
    )


# -------- 10.3: fetch a directory at a commit, return a zip -------- #
#
# Two-step API dance against GitHub's git-data API:
#
#   1. GET /repos/{owner}/{name}/git/trees/{commit_sha}?recursive=1
#      Returns every blob+tree in the commit. We filter to entries
#      under the agent's path.
#
#   2. For each blob entry: GET /repos/{owner}/{name}/git/blobs/{sha}
#      Returns base64 content. Decode and write into the zip.
#
# We use the git-data API (not the higher-level Contents API) because
# Contents bakes a 1MB inline limit per response and forces a follow-up
# fetch for anything larger; git-data uniformly returns base64 for any
# blob size, so the code path is the same for tiny and large files.

# Hard cap on the bundle size. The deploy upload path
# (POST /workspaces/me/deployments) caps multipart bodies at 10MB; we
# match that ceiling so a github_push deploy can never produce a blob
# the worker wouldn't accept via the CLI path. This also bounds the
# damage from a malicious or misconfigured agent path that points at
# an entire mono-repo.
MAX_GITHUB_DEPLOY_BYTES = 10 * 1024 * 1024


def _gh_headers(pat: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {pat}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lightsei-backend",
    }


def _gh_get(client: httpx.Client, url: str, *, pat: str) -> dict:
    """Single GET against GitHub. Translates non-2xx into GitHubAPIError
    so callers can distinguish auth/not_found/transport. Returns parsed
    JSON for the caller to walk."""
    try:
        r = client.get(url, headers=_gh_headers(pat))
    except httpx.TimeoutException as exc:
        raise GitHubAPIError(
            f"GitHub did not respond within {BACKGROUND_TIMEOUT_S}s",
            kind="transport",
        ) from exc
    except httpx.HTTPError as exc:
        raise GitHubAPIError(
            f"network error reaching GitHub: {type(exc).__name__}",
            kind="transport",
        ) from exc

    if r.status_code == 401:
        raise GitHubAPIError(
            "GitHub rejected the PAT (401) while fetching repo contents.",
            status=401, kind="auth",
        )
    if r.status_code == 403:
        raise GitHubAPIError(
            "GitHub returned 403 fetching repo contents — token scope or rate limit.",
            status=403, kind="auth",
        )
    if r.status_code == 404:
        raise GitHubAPIError(
            f"GitHub returned 404 for {url} — commit, path, or token access missing.",
            status=404, kind="not_found",
        )
    if not (200 <= r.status_code < 300):
        raise GitHubAPIError(
            f"GitHub returned {r.status_code}: {(r.text or '')[:200]}",
            status=r.status_code,
        )
    return r.json()


def fetch_directory_zip(
    *,
    repo_owner: str,
    repo_name: str,
    commit_sha: str,
    path: str,
    pat: str,
) -> bytes:
    """Build an in-memory zip of the repo subtree at `path` as of
    `commit_sha`. Returns the zip bytes; caller stores them in a
    DeploymentBlob row exactly like the CLI upload path does.

    The returned zip is rooted at `path/` exactly as the CLI bundles a
    directory — every entry is `<basename>/<rel/path>/file` so the
    Phase 5 worker's existing extract logic works without changes.

    Empty result → caller decides. We don't auto-fail on empty: a user
    could legitimately delete an entire agent directory in a commit
    and expect the redeploy attempt to land + fail cleanly downstream
    rather than be silently dropped here.

    Raises GitHubAPIError if the tree or any blob fetch fails.
    """
    import base64
    import io
    import zipfile

    # Normalize path: no leading/trailing slash for matching the tree
    # response, which uses repo-relative paths without a leading slash.
    norm_path = path.strip("/")
    prefix = norm_path + "/" if norm_path else ""

    tree_url = (
        f"{GITHUB_API_BASE}/repos/{repo_owner}/{repo_name}"
        f"/git/trees/{commit_sha}?recursive=1"
    )
    with httpx.Client(timeout=BACKGROUND_TIMEOUT_S) as client:
        tree = _gh_get(client, tree_url, pat=pat)

        # GitHub flags `truncated: true` on trees with >100k entries or
        # >7MB. We refuse to deploy anything that big — the cap matches
        # the multipart cap on the CLI upload path.
        if tree.get("truncated"):
            raise GitHubAPIError(
                "repo tree exceeds GitHub's recursive-tree limit; agent path "
                "is probably pointing at a directory that's too large to deploy",
                kind="github_api_error",
            )

        entries = tree.get("tree") or []
        # Filter to blobs under our path. A `path` of "" deploys the
        # whole repo; otherwise we want exact-match files OR anything
        # under prefix.
        wanted = []
        for e in entries:
            etype = e.get("type")
            epath = e.get("path") or ""
            if etype != "blob":
                continue
            if norm_path == "" or epath == norm_path or epath.startswith(prefix):
                wanted.append(e)

        # Build the zip. Stream blobs one at a time so we don't pin all
        # of them in memory simultaneously — the zipfile in-memory
        # buffer is the only "hold all of it" point.
        buf = io.BytesIO()
        running_size = 0
        with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            for e in wanted:
                blob_sha = e.get("sha")
                if not blob_sha:
                    continue
                blob_url = (
                    f"{GITHUB_API_BASE}/repos/{repo_owner}/{repo_name}"
                    f"/git/blobs/{blob_sha}"
                )
                blob = _gh_get(client, blob_url, pat=pat)
                if blob.get("encoding") != "base64":
                    raise GitHubAPIError(
                        f"unexpected blob encoding {blob.get('encoding')!r}",
                        kind="github_api_error",
                    )
                content_b64 = blob.get("content") or ""
                # GitHub returns base64 wrapped at 60 chars; b64decode
                # tolerates whitespace.
                data = base64.b64decode(content_b64)
                running_size += len(data)
                if running_size > MAX_GITHUB_DEPLOY_BYTES:
                    raise GitHubAPIError(
                        f"deploy exceeds {MAX_GITHUB_DEPLOY_BYTES} byte cap",
                        kind="github_api_error",
                    )
                # Strip the agent-path prefix so the zip looks identical
                # to a CLI bundle of just that directory: filenames
                # inside the zip are relative to the agent dir.
                arcname = (
                    e["path"][len(prefix):] if prefix and e["path"].startswith(prefix)
                    else e["path"]
                )
                # An empty arcname (when path == norm_path, i.e., a
                # single-file agent) shouldn't happen but guard anyway.
                if not arcname:
                    arcname = e["path"].rsplit("/", 1)[-1]
                zf.writestr(arcname, data)

    return buf.getvalue()
