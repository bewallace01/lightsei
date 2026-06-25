"""seo_render: render a drafted page into a file for a target framework."""
from __future__ import annotations

import pytest

import seo_render


_PAGE = {
    "title": "Restaurant Inventory Tips",
    "meta_description": "Cut food costs with these tips.",
    "slug": "restaurant-inventory-tips",
    "h1": "Restaurant Inventory Management Tips",
    "body_html": "<h2>Why it matters</h2><p>Food cost is everything.</p>",
}


def test_render_html_is_full_document():
    r = seo_render.render_page(_PAGE, "html")
    assert r["content"].startswith("<!doctype html>")
    assert "<title>Restaurant Inventory Tips</title>" in r["content"]
    assert '<meta name="description" content="Cut food costs with these tips.">' in r["content"]
    assert "<h1>Restaurant Inventory Management Tips</h1>" in r["content"]
    assert r["path"] == "public/pages/restaurant-inventory-tips.html"


def test_render_markdown_has_front_matter():
    r = seo_render.render_page(_PAGE, "markdown")
    assert r["content"].startswith("---\n")
    assert 'title: "Restaurant Inventory Tips"' in r["content"]
    assert 'description: "Cut food costs with these tips."' in r["content"]
    assert "# Restaurant Inventory Management Tips" in r["content"]
    assert r["path"] == "content/restaurant-inventory-tips.md"


def test_render_mdx_path():
    r = seo_render.render_page(_PAGE, "mdx")
    assert r["path"] == "src/content/restaurant-inventory-tips.mdx"
    assert r["content"].startswith("---\n")  # same front-matter body as markdown


def test_render_unknown_format_raises():
    with pytest.raises(ValueError):
        seo_render.render_page(_PAGE, "pdf")


def test_render_slugifies_when_slug_missing():
    r = seo_render.render_page({"h1": "Hello World!", "body_html": "x"}, "markdown")
    assert r["path"] == "content/hello-world.md"


def test_render_escapes_yaml_quotes():
    r = seo_render.render_page(
        {"title": 'A "quoted" title', "meta_description": "d", "slug": "s",
         "h1": "H", "body_html": "x"}, "markdown")
    # The embedded quote is escaped so the front matter stays valid YAML.
    assert 'title: "A \\"quoted\\" title"' in r["content"]


def test_render_escapes_html():
    r = seo_render.render_page(
        {"title": "A & B <x>", "meta_description": "d", "slug": "s",
         "h1": "H", "body_html": "p"}, "html")
    assert "A &amp; B &lt;x&gt;" in r["content"]


def test_render_next_app_router():
    r = seo_render.render_page(_PAGE, "next-app")
    assert r["path"] == "app/restaurant-inventory-tips/page.tsx"
    c = r["content"]
    # Real SEO metadata export + a default-exported server component.
    assert 'import type { Metadata } from "next";' in c
    assert 'export const metadata: Metadata = {' in c
    assert '"Restaurant Inventory Tips"' in c          # title as a JS string
    assert "export default function Page()" in c
    assert "dangerouslySetInnerHTML" in c


def test_render_next_pages_router():
    r = seo_render.render_page(_PAGE, "next-pages")
    assert r["path"] == "pages/restaurant-inventory-tips.tsx"
    c = r["content"]
    assert 'import Head from "next/head";' in c
    assert "<title>" in c and "dangerouslySetInnerHTML" in c


def test_render_next_app_embeds_body_as_safe_js_string():
    # Body HTML with quotes / a script-like tag must be a valid JS string
    # literal (json-escaped), not break the .tsx module.
    r = seo_render.render_page(
        {"title": "T", "meta_description": "d", "slug": "s", "h1": "H",
         "body_html": '<p class="lead">a "quote" & <b>x</b></p>'}, "next-app")
    c = r["content"]
    # The double-quotes inside the body are backslash-escaped in the JS string.
    assert '\\"quote\\"' in c
    # And it round-trips: extract the __html string literal and json-load it.
    import json, re
    m = re.search(r"__html:\s*(\".*?\")\s*\}\}", c)
    assert m and json.loads(m.group(1)) == '<p class="lead">a "quote" & <b>x</b></p>'
