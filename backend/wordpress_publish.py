"""Publish a generated SEO page to a WordPress site via the REST API.

The git-deploy path (github_publish) covers sites hosted FROM a repo
(Vercel/Cloudflare/Railway/Netlify). WordPress is the other big family: the
site isn't a git repo, so there's no PR to open. Instead we create the page
through the WordPress REST API (`POST /wp-json/wp/v2/pages`), authenticating
with an Application Password (the standard for programmatic WordPress access,
sent as HTTP Basic auth).

The page is created as a **draft** by default, so the owner reviews and
publishes it in wp-admin: the draft is the review gate, mirroring the PR gate
on the git path.

Pure + testable: the single HTTP call goes through an injected `request` seam,
so payload-building and response-interpretation unit-test without network.
`_httpx_request` is the production implementation.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional
from urllib.parse import urlparse

# request(method, url, *, username, app_password, json=None) -> {"status": int, "body": dict}
WordPressRequest = Callable[..., dict[str, Any]]

VALID_STATUSES = ("draft", "publish")


class WordPressPublishError(Exception):
    """A WordPress REST step failed; the message is owner-facing-ish."""


def normalize_base_url(url: str) -> Optional[str]:
    """A site's base URL as `scheme://host[/path]` with no trailing slash, or
    None if there's no usable host. Adds https:// when missing and strips a
    trailing `/wp-json...` so a pasted API root still resolves to the site
    root."""
    u = (url or "").strip()
    if not u:
        return None
    if not re.match(r"^https?://", u, re.IGNORECASE):
        u = "https://" + u
    p = urlparse(u)
    if not p.netloc:
        return None
    path = p.path.rstrip("/")
    path = re.sub(r"/wp-json(/.*)?$", "", path, flags=re.IGNORECASE)
    return f"{p.scheme}://{p.netloc}{path}"


def page_payload(page: dict[str, Any], status: str = "draft") -> dict[str, Any]:
    """The wp/v2/pages request body from the drafted page fields. Title +
    content are required by the caller; slug + excerpt (meta description) are
    included only when present."""
    payload: dict[str, Any] = {
        "title": str(page.get("title") or page.get("h1") or "").strip(),
        "content": str(page.get("body_html") or ""),
        "status": status,
    }
    slug = str(page.get("slug") or "").strip()
    if slug:
        payload["slug"] = slug
    meta = str(page.get("meta_description") or "").strip()
    if meta:
        payload["excerpt"] = meta
    return payload


def publish_page_to_wordpress(
    *,
    request: WordPressRequest,
    base_url: str,
    username: str,
    app_password: str,
    page: dict[str, Any],
    status: str = "draft",
) -> dict[str, Any]:
    """Create the page on the WordPress site and return {id, link, status,
    edit_url}. Raises WordPressPublishError with a clear message on a bad
    request or a failed REST call. Defaults to a draft so the owner reviews it
    in wp-admin before it goes live."""
    if status not in VALID_STATUSES:
        raise WordPressPublishError(
            f"status must be one of {VALID_STATUSES}, got {status!r}")
    base = normalize_base_url(base_url)
    if not base:
        raise WordPressPublishError("enter a valid WordPress site URL")
    if not (username or "").strip() or not (app_password or "").strip():
        raise WordPressPublishError(
            "WordPress username + application password are required")

    payload = page_payload(page, status)
    if not payload["title"]:
        raise WordPressPublishError("the page needs a title")
    if not str(payload.get("content") or "").strip():
        raise WordPressPublishError("the page needs content")

    url = f"{base}/wp-json/wp/v2/pages"
    res = request(method="POST", url=url, username=username,
                  app_password=app_password, json=payload)
    st = int(res.get("status", 0))
    body = res.get("body") if isinstance(res.get("body"), dict) else {}

    if st in (200, 201):
        page_id = body.get("id")
        return {
            "id": page_id,
            "link": body.get("link"),
            "status": body.get("status") or status,
            # The wp-admin edit screen, so the owner can jump straight to review.
            "edit_url": f"{base}/wp-admin/post.php?post={page_id}&action=edit" if page_id else None,
        }
    if st in (401, 403):
        raise WordPressPublishError(
            "WordPress rejected the credentials. Check the username and the "
            "application password (Users -> Profile -> Application Passwords).")
    if st == 404:
        raise WordPressPublishError(
            "couldn't reach the WordPress REST API at that URL. Confirm it's a "
            "WordPress site and the REST API isn't disabled.")
    msg = body.get("message") if isinstance(body, dict) else None
    raise WordPressPublishError(
        f"WordPress publish failed (HTTP {st})" + (f": {msg}" if msg else ""))


def _httpx_request(*, method: str, url: str, username: str, app_password: str,
                   json: Optional[dict] = None) -> dict[str, Any]:
    import base64

    import httpx

    token = base64.b64encode(
        f"{username}:{app_password}".encode("utf-8")).decode("ascii")
    resp = httpx.request(
        method, url, json=json, timeout=20.0,
        headers={
            "Authorization": f"Basic {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Lightsei-SEO/1.0",
        },
    )
    try:
        body = resp.json()
    except Exception:
        body = {}
    return {"status": resp.status_code, "body": body if isinstance(body, dict) else {}}
