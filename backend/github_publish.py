"""Publish a generated page into a connected GitHub repo as a PR.

The git-deploy path for SEO pages: rather than a connector per hosting
platform, we commit the page to the owner's GitHub repo and open a pull
request. Cloudflare Pages, Vercel, Railway, and Netlify all auto-deploy
from git, so one PR covers every git-hosted platform, and the PR is the
owner's review gate before anything goes live.

The four GitHub REST calls (read base branch, create branch, commit file,
open PR) go through an injected `request` seam so the orchestration is
pure and unit-testable without network. `_httpx_request` is the
production implementation.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Callable, Optional

# request(method, url, token, json=None) -> {"status": int, "body": dict}
GithubRequest = Callable[..., dict[str, Any]]

GITHUB_API = "https://api.github.com"


class GithubPublishError(Exception):
    """A GitHub API step failed; the message is owner-facing-ish."""


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def branch_name_for(slug: str) -> str:
    """A deterministic branch name for a page slug (no randomness, so a
    re-publish of the same page collides loudly rather than spamming
    branches)."""
    clean = _SLUG_RE.sub("-", (slug or "page").lower()).strip("-") or "page"
    return f"lightsei-seo/{clean}"


def is_safe_repo_path(path: str) -> bool:
    """A repo-relative file path that can't escape the repo."""
    p = (path or "").strip()
    if not p or p.startswith("/") or p.startswith("~"):
        return False
    parts = p.split("/")
    return ".." not in parts and "" not in parts[:-1]


def _call(request: GithubRequest, token: str, method: str, url: str,
          json: Optional[dict] = None) -> tuple[int, Any]:
    """Returns (status, body). Body is usually a dict; the PR-list endpoint
    returns a JSON array, so the type is Any (callers that expect a dict use
    .get and only hit dict-returning endpoints)."""
    res = request(method=method, url=url, token=token, json=json)
    body = res.get("body")
    return int(res.get("status", 0)), (body if body is not None else {})


def find_open_pr_for_branch(
    request: GithubRequest, token: str, *, owner: str, repo: str, branch: str,
) -> Optional[dict[str, Any]]:
    """The open PR whose head is `branch` (same-repo), or None. Used to turn a
    'branch already exists' collision into a pointer to the PR that's already
    open for this page, instead of a dead-end error."""
    api = f"{GITHUB_API}/repos/{owner}/{repo}"
    st, body = _call(request, token, "GET",
                     f"{api}/pulls?state=open&head={owner}:{branch}")
    if st == 200 and isinstance(body, list) and body:
        pr = body[0]
        if isinstance(pr, dict):
            return {"pr_url": pr.get("html_url"), "pr_number": pr.get("number"),
                    "branch": branch}
    return None


def _commit_file(request: GithubRequest, token: str, api: str, *, path: str,
                 content: str, branch: str, message: str) -> None:
    """Create or update one file on `branch` via the Contents API. Updating an
    existing file requires its current blob sha, so look it up first (404 =
    new file, no sha needed)."""
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    payload: dict[str, Any] = {"message": message, "content": encoded, "branch": branch}
    st, body = _call(request, token, "GET", f"{api}/contents/{path}?ref={branch}")
    if st == 200 and isinstance(body, dict) and body.get("sha"):
        payload["sha"] = body["sha"]  # update in place
    st, body = _call(request, token, "PUT", f"{api}/contents/{path}", payload)
    if st not in (200, 201):
        raise GithubPublishError(
            f"couldn't commit {path!r} (HTTP {st}): {body.get('message')}")


def publish_page_to_repo(
    *,
    request: GithubRequest,
    token: str,
    owner: str,
    repo: str,
    base_branch: str,
    path: str,
    content: str,
    branch_name: str,
    commit_message: str,
    pr_title: str,
    pr_body: str,
    extra_files: Optional[list[tuple[str, str]]] = None,
) -> dict[str, Any]:
    """Create `branch_name` off `base_branch`, commit `content` to `path`,
    commit any `extra_files` [(path, content), ...] on the same branch, and
    open a PR. Returns {pr_url, pr_number, branch}. Raises GithubPublishError
    with a clear message on any failed step.

    `extra_files` lets a publish carry companion edits (e.g. registering the
    page's route in App.tsx) so the page goes fully live on merge.
    """
    if not is_safe_repo_path(path):
        raise GithubPublishError(f"unsafe repo path: {path!r}")
    for ep, _ in (extra_files or []):
        if not is_safe_repo_path(ep):
            raise GithubPublishError(f"unsafe repo path: {ep!r}")
    api = f"{GITHUB_API}/repos/{owner}/{repo}"

    # 1. Resolve the base branch's head commit sha.
    st, body = _call(request, token, "GET", f"{api}/git/ref/heads/{base_branch}")
    if st != 200:
        raise GithubPublishError(
            f"couldn't read base branch {base_branch!r} (HTTP {st})")
    base_sha = (((body.get("object") or {}).get("sha")) or "")
    if not base_sha:
        raise GithubPublishError("base branch had no commit sha")

    # 2. Create the new branch ref.
    st, body = _call(request, token, "POST", f"{api}/git/refs",
                     {"ref": f"refs/heads/{branch_name}", "sha": base_sha})
    if st == 422:
        # The deterministic branch already exists: a publish for this page is
        # already open. Point the owner at that PR instead of erroring out.
        existing = find_open_pr_for_branch(
            request, token, owner=owner, repo=repo, branch=branch_name)
        if existing is not None:
            return {**existing, "already_open": True}
        raise GithubPublishError(
            f"branch {branch_name!r} already exists but has no open PR — it may "
            "have been merged already, or be left over from a closed PR. Delete "
            "that branch on GitHub to republish this page.")
    if st not in (200, 201):
        raise GithubPublishError(
            f"couldn't create branch {branch_name!r} (HTTP {st}): {body.get('message')}")

    # 3. Commit the page file, then any companion files (e.g. App.tsx route
    #    registration) on the same branch.
    _commit_file(request, token, api, path=path, content=content,
                 branch=branch_name, message=commit_message)
    for ep, ec in (extra_files or []):
        _commit_file(request, token, api, path=ep, content=ec,
                     branch=branch_name, message=f"{commit_message} (wire up {ep})")

    # 4. Open the PR.
    st, body = _call(request, token, "POST", f"{api}/pulls",
                     {"title": pr_title, "head": branch_name,
                      "base": base_branch, "body": pr_body})
    if st not in (200, 201):
        raise GithubPublishError(
            f"committed the page but couldn't open the PR (HTTP {st}): {body.get('message')}")

    return {
        "pr_url": body.get("html_url"),
        "pr_number": body.get("number"),
        "branch": branch_name,
    }


def _httpx_request(*, method: str, url: str, token: str,
                   json: Optional[dict] = None) -> dict[str, Any]:
    import httpx

    resp = httpx.request(
        method, url, json=json, timeout=20.0,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Lightsei-SEO/1.0",
        },
    )
    try:
        body = resp.json()
    except Exception:
        body = {}
    # Most endpoints return an object; the PR-list endpoint returns an array.
    # Preserve both (other shapes -> {}), so find_open_pr_for_branch can read it.
    return {"status": resp.status_code,
            "body": body if isinstance(body, (dict, list)) else {}}


def fetch_file(*, request: GithubRequest, token: str, owner: str, repo: str,
               path: str, ref: str = "HEAD") -> Optional[str]:
    """Read a text file from a repo (the Contents API, base64-decoded). Returns
    the file's text, or None if it isn't there / isn't readable. Used to pull
    an existing page as a template. `request` is the injectable seam."""
    import base64
    if not is_safe_repo_path(path):
        return None
    api = f"{GITHUB_API}/repos/{owner}/{repo}"
    st, body = _call(request, token, "GET", f"{api}/contents/{path}?ref={ref}")
    if st != 200:
        return None
    if body.get("encoding") == "base64" and body.get("content"):
        try:
            return base64.b64decode(body["content"]).decode("utf-8", "ignore")
        except Exception:
            return None
    # Some responses inline content without base64 for small files.
    c = body.get("content")
    return c if isinstance(c, str) else None


def list_page_files(*, request: GithubRequest, token: str, owner: str, repo: str,
                    ref: str = "HEAD", limit: int = 60) -> list[str]:
    """List likely page-source files in the repo (for the template picker):
    files under a pages/ or routes/ directory, or *Page.* files. Best-effort;
    returns [] on any failure."""
    api = f"{GITHUB_API}/repos/{owner}/{repo}"
    st, body = _call(request, token, "GET", f"{api}/git/trees/{ref}?recursive=1")
    if st != 200:
        return []
    out: list[str] = []
    exts = (".tsx", ".jsx", ".vue", ".svelte", ".astro", ".html", ".js", ".ts")
    for node in (body.get("tree") or []):
        if node.get("type") != "blob":
            continue
        p = node.get("path") or ""
        low = p.lower()
        if not low.endswith(exts):
            continue
        if ("/pages/" in low or low.startswith("pages/") or "/routes/" in low
                or low.startswith("routes/") or "page." in low
                or low.endswith("page.tsx") or low.endswith("page.jsx")):
            out.append(p)
        if len(out) >= limit:
            break
    return out
