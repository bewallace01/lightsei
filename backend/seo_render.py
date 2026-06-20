"""Render a generated SEO page into a file for a target site framework.

Spica drafts a page as fields (title / meta_description / h1 / body_html /
slug). Different sites want different files: a static/HTML host wants a full
HTML document, a Markdown SSG (Hugo, Astro, Eleventy, Jekyll) wants a
front-matter `.md`, an MDX site wants `.mdx`. This picks the right content +
a sensible default repo path per format so the committed page actually
renders, instead of always shipping raw HTML.

Pure + testable. `render_page(page, fmt) -> {content, path}`.
"""
from __future__ import annotations

import re
from typing import Any

FORMATS = ("html", "markdown", "mdx")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    return _SLUG_RE.sub("-", (s or "").lower()).strip("-") or "page"


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _yaml_quote(s: str) -> str:
    # Double-quoted YAML scalar; escape backslash + quote.
    return '"' + (s or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _render_html(p: dict[str, Any]) -> str:
    return "\n".join([
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_html_escape(p.get('title',''))}</title>",
        f'<meta name="description" content="{_html_escape(p.get("meta_description",""))}">',
        "</head>",
        "<body>",
        f"<h1>{_html_escape(p.get('h1',''))}</h1>",
        p.get("body_html", "") or "",
        "</body>",
        "</html>",
        "",
    ])


def _render_markdown(p: dict[str, Any]) -> str:
    # YAML front matter + H1 + the body (raw HTML is valid in Markdown/MDX).
    return "\n".join([
        "---",
        f"title: {_yaml_quote(p.get('title',''))}",
        f"description: {_yaml_quote(p.get('meta_description',''))}",
        "---",
        "",
        f"# {p.get('h1','')}",
        "",
        p.get("body_html", "") or "",
        "",
    ])


def render_page(page: dict[str, Any], fmt: str) -> dict[str, str]:
    """Return {content, path} for the page in the requested format. Raises
    ValueError for an unknown format."""
    fmt = (fmt or "html").lower()
    if fmt not in FORMATS:
        raise ValueError(f"unknown format {fmt!r}; expected one of {FORMATS}")
    slug = _slugify(str(page.get("slug") or page.get("h1") or "page"))

    if fmt == "html":
        return {"content": _render_html(page), "path": f"public/pages/{slug}.html"}
    if fmt == "markdown":
        return {"content": _render_markdown(page), "path": f"content/{slug}.md"}
    # mdx
    return {"content": _render_markdown(page), "path": f"src/content/{slug}.mdx"}
