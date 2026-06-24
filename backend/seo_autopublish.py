"""Spica auto-opens the publish PR (Phase 37.10).

When a workspace has opted into auto-publishing and chosen a target repo, a
new SEO page draft (the `seo.page_drafted` event) opens a pull request
automatically, reusing the same git-deploy publish path the owner's manual
/seo button uses. The PR is still the review gate: nothing reaches the live
site without a merge, and the per-workspace opt-in is the standing
authorization to open PRs on the owner's behalf.

The orchestration (render -> optional App.tsx route wiring -> open PR) is pure:
GitHub access goes through an injected `request` seam (the same one
github_publish uses), so the whole flow unit-tests without network or a
database. The thin DB-bound parts (reading the workspace setting, resolving the
repo token, scheduling the background task) live in main.py and call in here.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import github_publish
import seo_render

logger = logging.getLogger(__name__)

PAGE_DRAFTED_KIND = "seo.page_drafted"


def should_autopublish(
    *, enabled: bool, repo_id: Optional[str], event_kind: str,
) -> bool:
    """True only when the workspace opted in, picked a target repo, and the
    event is a fresh page draft. Pure: no DB, no network."""
    return bool(enabled) and bool(repo_id) and event_kind == PAGE_DRAFTED_KIND


def page_from_event(payload: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Pull the page dict out of a `seo.page_drafted` payload.

    Spica emits `{command_id, keyword, page: {title, meta_description, slug,
    h1, body_html}, ...}`. Older / hand-built shapes may carry the page fields
    at the top level, so fall back to the payload itself. Returns None when
    there's no usable page (no title and no body to publish), so the caller
    skips rather than opening an empty PR.
    """
    payload = payload or {}
    page = payload.get("page")
    if not isinstance(page, dict):
        page = payload
    title = str(page.get("title") or page.get("h1") or "").strip()
    body = str(page.get("body_html") or "").strip()
    if not title or not body:
        return None
    return page


def title_for(page: dict[str, Any]) -> str:
    """The human title used for the commit / PR / branch name."""
    return str(page.get("title") or page.get("h1") or "Untitled page").strip() or "Untitled page"


def _wire_route(
    request, token, owner, repo, base_branch, path, content,
) -> tuple[list[tuple[str, str]], Optional[str]]:
    """Best-effort: for a React page under src/pages/*, register its lazy
    import + <Route> in the repo's central App.tsx so the page goes live on
    merge, not just exists in the repo. Returns (extra_files, routed_path).
    Any failure leaves both empty (publish the page file alone)."""
    extra_files: list[tuple[str, str]] = []
    routed_path: Optional[str] = None
    if not (path.startswith("src/pages/") and path.endswith((".tsx", ".jsx"))):
        return extra_files, routed_path
    try:
        import react_router_wiring
        for app_path in ("src/App.tsx", "src/App.jsx"):
            app_src = github_publish.fetch_file(
                request=request, token=token, owner=owner, repo=repo,
                path=app_path, ref=base_branch)
            if not app_src:
                continue
            plan = react_router_wiring.plan_route_wiring(
                app_source=app_src, page_content=content, file_path=path)
            if plan:
                extra_files.append((app_path, plan["app_source"]))
                routed_path = plan["route_path"]
            break
    except Exception:
        logger.exception("autopublish: route wiring skipped")
    return extra_files, routed_path


def orchestrate_publish(
    *,
    request,
    token: str,
    owner: str,
    repo: str,
    base_branch: str,
    title: str,
    page: Optional[dict[str, Any]] = None,
    fmt: str = "html",
    content: Optional[str] = None,
    path: Optional[str] = None,
    pr_body: Optional[str] = None,
    branch_name: Optional[str] = None,
) -> dict[str, Any]:
    """Render (or take direct content), wire the App.tsx route when applicable,
    and open the PR. Shared by the manual publish endpoint and the auto-publish
    background task so both behave identically.

    Supply the file one of two ways: a structured `page` + `fmt`
    (html/markdown/mdx, rendered to a sensible default path), or pre-built
    `content` + `path`. Raises ValueError on a bad render request / unsafe path
    / empty content, and github_publish.GithubPublishError on a failed GitHub
    step (e.g. the branch already exists because the draft was re-emitted).
    """
    if page is not None:
        rendered = seo_render.render_page(page, fmt or "html")
        content = rendered["content"]
        path = path or rendered["path"]
    else:
        content = content or ""
        path = path or ""

    if not github_publish.is_safe_repo_path(path):
        raise ValueError("path must be a repo-relative file path (no leading slash or ..)")
    if not str(content or "").strip():
        raise ValueError("content is required")

    extra_files, routed_path = _wire_route(
        request, token, owner, repo, base_branch, path, content)

    branch = branch_name or github_publish.branch_name_for(title)
    body = pr_body or (
        "New SEO page drafted by Spica (Lightsei). Review the page, then merge "
        "to publish it on your site.")
    if routed_path:
        body += (
            f"\n\nThis PR also registers the page's route ({routed_path}) in "
            "App.tsx, so it goes live as soon as you merge.")

    result = github_publish.publish_page_to_repo(
        request=request, token=token, owner=owner, repo=repo,
        base_branch=base_branch, path=path, content=content,
        branch_name=branch, commit_message=f"Add page: {title}",
        pr_title=f"Add SEO page: {title}", pr_body=body,
        extra_files=extra_files)
    if routed_path:
        result["routed_path"] = routed_path
    return result
