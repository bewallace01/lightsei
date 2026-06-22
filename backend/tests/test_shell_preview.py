"""Tests for shell_preview: borrow a live page's shell, swap in new content."""
import shell_preview as sp


_SHELL = """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="/assets/site.css">
</head><body>
<header><nav>Home | Working Capital</nav></header>
<main class="page-main">
  <h1>When You Need Restaurant Working Capital</h1>
  <p>Old content here.</p>
</main>
<footer>© Restaurant Owner's Guide</footer>
<script src="/assets/app.js"></script>
<script>window.__hydrate = true;</script>
</body></html>"""

_PAGE = {"h1": "Restaurant Inventory Tips",
         "body_html": "<p>Count inventory weekly.</p>"}


def test_origin_of():
    assert sp.origin_of("https://x.com/a/b?q=1") == "https://x.com"
    assert sp.origin_of("not a url") == ""


def test_strip_scripts_removes_all():
    out = sp.strip_scripts(_SHELL)
    assert "<script" not in out.lower()


def test_inject_base_added_once():
    out = sp.inject_base(_SHELL, "https://therestaurantownersguide.com")
    assert out.count("<base ") == 1
    assert '<base href="https://therestaurantownersguide.com/">' in out
    # Idempotent: a second pass doesn't add another.
    assert sp.inject_base(out, "https://therestaurantownersguide.com").count("<base ") == 1


def test_build_preview_swaps_main_keeps_shell():
    res = sp.build_preview(
        _SHELL, page=_PAGE,
        shell_url="https://therestaurantownersguide.com/restaurant-working-capital")
    html = res["html"]
    assert res["swapped"] is True
    # New content in, old content out.
    assert "Restaurant Inventory Tips" in html
    assert "Count inventory weekly." in html
    assert "When You Need Restaurant Working Capital" not in html
    assert "Old content here." not in html
    # Shell preserved: nav, footer, stylesheet link.
    assert "<nav>" in html and "<footer>" in html
    assert 'href="/assets/site.css"' in html
    # Scripts stripped, base added for off-site asset loading.
    assert "<script" not in html.lower()
    assert '<base href="https://therestaurantownersguide.com/">' in html


def test_build_preview_no_main_reports_not_swapped():
    res = sp.build_preview(
        "<html><head></head><body><div>no main here</div></body></html>",
        page=_PAGE, shell_url="https://x.com/p")
    assert res["swapped"] is False
    # Still returns usable html (shell unchanged apart from script strip/base).
    assert "<body>" in res["html"]


def test_content_fragment_uses_h1_then_body():
    frag = sp.content_fragment({"h1": "Title", "body_html": "<p>Body</p>"})
    assert frag.index("Title") < frag.index("Body")
    # Falls back to title when no h1.
    assert "Alt" in sp.content_fragment({"title": "Alt", "body_html": ""})
