"""Detect a connected repo's web framework, to default the publish format.

Spica can render a drafted page for several targets (static HTML, Markdown,
Next.js App/Pages Router). Rather than make the owner know which their repo
uses, we sniff it from the repo's package.json dependencies + its page-file
layout and suggest the right format.

Pure + testable: `detect_framework(page_paths, package_json)` takes data the
caller already fetches (the page-file list + package.json text) and returns a
framework string; `default_format_for(framework)` maps it to a seo_render
format (or None when we shouldn't override the default).
"""
from __future__ import annotations

import json
import re
from typing import Optional

# `app/.../page.tsx` is the Next.js App Router signature (Remix uses app/routes,
# Astro uses src/pages, Vite+RR uses src/pages components — none use this).
_APP_PAGE_RE = re.compile(r"(?:^|/)(?:src/)?app/.*\bpage\.[jt]sx?$", re.IGNORECASE)
# `pages/foo.tsx` is Pages Router — but Vite+React-Router also uses src/pages,
# so this only means "Next Pages Router" when `next` is also a dependency.
_PAGES_RE = re.compile(r"(?:^|/)(?:src/)?pages/.+\.[jt]sx?$", re.IGNORECASE)


def _dependency_names(package_json: Optional[str]) -> set[str]:
    """Every declared dependency name (prod + dev + peer). Empty set if the
    package.json is missing or unparseable (caller falls back to path sniffing)."""
    if not package_json:
        return set()
    try:
        obj = json.loads(package_json)
    except Exception:
        return set()
    names: set[str] = set()
    if isinstance(obj, dict):
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            d = obj.get(key)
            if isinstance(d, dict):
                names.update(d.keys())
    return names


def detect_framework(
    page_paths: Optional[list[str]], package_json: Optional[str],
) -> str:
    """One of: 'next-app', 'next-pages', 'vite-react', 'static', 'unknown'.

    Next.js is identified by the `next` dependency OR the App Router's
    `app/**/page.tsx` signature (which no other framework uses). Pages Router is
    only claimed when `next` is present, since Vite + React Router also keeps
    components under src/pages.
    """
    deps = _dependency_names(package_json)
    paths = page_paths or []
    has_app = any(_APP_PAGE_RE.search(p) for p in paths)
    has_pages = any(_PAGES_RE.search(p) for p in paths)

    if "next" in deps or has_app:
        if has_app:
            return "next-app"
        if has_pages:
            return "next-pages"
        return "next-app"  # next present but no page detected yet -> modern default

    if "react-router-dom" in deps or "vite" in deps:
        return "vite-react"

    if package_json:
        return "unknown"  # a JS project we don't specifically recognize
    return "static"        # no package.json -> treat as a plain static site


def default_format_for(framework: str) -> Optional[str]:
    """The seo_render format to default for a detected framework, or None when
    we shouldn't override the owner's current choice (vite-react publishes via
    component mode, not a render format; static/unknown keep the HTML default)."""
    return {"next-app": "next-app", "next-pages": "next-pages"}.get(framework)
