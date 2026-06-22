"""Render a near-exact visual preview of a repo-matched page.

A "component" page (a React .tsx that imports the site's shared components)
can't be rendered standalone, so there's nothing to show the owner before they
publish. But the site is pre-rendered: any live page already carries the real
shell (header/nav, hero, footer) and the real stylesheets. So we build a
preview by taking a live page from the same site as a SHELL and swapping its
<main> content for the new page's content.

Steps (all pure except the caller's fetch):
  1. Strip <script> tags so the page's own JS can't re-hydrate and wipe the
     swapped-in content (and so analytics/side effects don't fire).
  2. Add <base href="origin/"> so the shell's relative CSS/images/fonts still
     load when the HTML is viewed off-site.
  3. Replace the inner HTML of <main> with the new content fragment.

Returns the original shell unchanged if it has no <main> we can target (the
caller can still show it, or fall back).
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.IGNORECASE | re.DOTALL)
_SCRIPT_SELFCLOSE_RE = re.compile(r"<script\b[^>]*/>", re.IGNORECASE)
_MAIN_RE = re.compile(r"(<main\b[^>]*>)(.*?)(</main>)", re.IGNORECASE | re.DOTALL)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BASE_RE = re.compile(r"<base\b[^>]*>", re.IGNORECASE)
# A `crossorigin` attribute makes the browser CORS-gate the resource. The
# shell's stylesheet uses it; loaded cross-origin from the preview, the CSS
# would be blocked (the site doesn't send CORS headers for it). Strip it so the
# CSS loads as a normal no-CORS resource (which still applies).
_CROSSORIGIN_RE = re.compile(r"\s+crossorigin(=([\"'])[^\"']*\2|=\S+)?", re.IGNORECASE)
# Root-relative href/src ( /foo, not //cdn ) -> absolute against the origin, so
# assets resolve to the owner's site rather than the preview's blob origin.
_ROOT_REL_ATTR_RE = re.compile(r'\b(href|src)=([\"\'])(/(?!/)[^\"\']*)\2', re.IGNORECASE)


def origin_of(url: str) -> str:
    """scheme://host[:port] for a URL, or '' if it can't be parsed."""
    try:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    except Exception:
        pass
    return ""


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def content_fragment(page: dict[str, Any]) -> str:
    """The new page's body as semantic HTML to drop into the shell's <main>:
    the H1 followed by the body. Styling comes from the shell's own CSS."""
    h1 = _html_escape(str(page.get("h1") or page.get("title") or ""))
    body = str(page.get("body_html") or "")
    parts = []
    if h1:
        parts.append(f"<h1>{h1}</h1>")
    parts.append(body)
    return "\n".join(parts)


def strip_scripts(html: str) -> str:
    html = _SCRIPT_RE.sub("", html or "")
    html = _SCRIPT_SELFCLOSE_RE.sub("", html)
    return html


def strip_crossorigin(html: str) -> str:
    """Remove `crossorigin` attributes so cross-origin stylesheets (and other
    assets) load without CORS enforcement and actually apply in the preview."""
    return _CROSSORIGIN_RE.sub("", html or "")


def absolutize_root_relative(html: str, origin: str) -> str:
    """Rewrite root-relative href/src (/assets/x.css) to absolute against the
    origin, so assets load from the owner's site rather than the preview's
    blob: origin. Leaves protocol-relative (//cdn) and absolute URLs alone."""
    if not origin:
        return html or ""
    return _ROOT_REL_ATTR_RE.sub(
        lambda m: f'{m.group(1)}={m.group(2)}{origin}{m.group(3)}{m.group(2)}',
        html or "")


def inject_base(html: str, origin: str) -> str:
    """Ensure a <base href="origin/"> right after <head> so the shell's
    relative asset URLs resolve off-site. No-op if a <base> already exists or
    there's no <head>."""
    if not origin or _BASE_RE.search(html or ""):
        return html
    m = _HEAD_OPEN_RE.search(html or "")
    if not m:
        return html
    tag = f'<base href="{origin}/">'
    return html[: m.end()] + tag + html[m.end():]


def swap_main(html: str, fragment: str) -> tuple[str, bool]:
    """Replace the inner HTML of the first <main>. Returns (html, swapped)."""
    swapped = False

    def _repl(m: "re.Match[str]") -> str:
        nonlocal swapped
        swapped = True
        return m.group(1) + "\n" + fragment + "\n" + m.group(3)

    out = _MAIN_RE.sub(_repl, html or "", count=1)
    return out, swapped


def build_preview(shell_html: str, *, page: dict[str, Any], shell_url: str) -> dict[str, Any]:
    """Produce {html, swapped}: the shell with scripts stripped, a <base> added,
    and <main> swapped for the new page's content. `swapped` is False if no
    <main> was found (the html is still returned, shell unchanged otherwise)."""
    origin = origin_of(shell_url)
    html = strip_scripts(shell_html or "")
    html = strip_crossorigin(html)
    html = absolutize_root_relative(html, origin)
    html = inject_base(html, origin)
    html, swapped = swap_main(html, content_fragment(page))
    return {"html": html, "swapped": swapped}
