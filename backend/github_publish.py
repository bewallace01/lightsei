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
          json: Optional[dict] = None) -> tuple[int, dict[str, Any]]:
    res = request(method=method, url=url, token=token, json=json)
    return int(res.get("status", 0)), (res.get("body") or {})


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
) -> dict[str, Any]:
    """Create `branch_name` off `base_branch`, commit `content` to `path`,
    and open a PR. Returns {pr_url, pr_number, branch}. Raises
    GithubPublishError with a clear message on any failed step.
    """
    if not is_safe_repo_path(path):
        raise GithubPublishError(f"unsafe repo path: {path!r}")
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
        raise GithubPublishError(
            f"branch {branch_name!r} already exists — an unmerged publish for "
            "this page is likely already open.")
    if st not in (200, 201):
        raise GithubPublishError(
            f"couldn't create branch {branch_name!r} (HTTP {st}): {body.get('message')}")

    # 3. Commit the page file (Contents API; base64-encoded body).
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    st, body = _call(request, token, "PUT", f"{api}/contents/{path}",
                     {"message": commit_message, "content": encoded,
                      "branch": branch_name})
    if st not in (200, 201):
        raise GithubPublishError(
            f"couldn't commit {path!r} (HTTP {st}): {body.get('message')}")

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
    return {"status": resp.status_code, "body": body if isinstance(body, dict) else {}}
