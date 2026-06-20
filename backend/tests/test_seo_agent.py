"""SEO assistant tests.

Drives agents/seo/bot.py's pure audit + page-generation + tick in
isolation with a stubbed `lightsei` module, an injected fake fetcher, and
a fake Anthropic client factory (same seams as the website + marketing
assistants).
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SEO_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "seo" / "bot.py"
).resolve()


@pytest.fixture()
def fake_lightsei(monkeypatch):
    fake = types.ModuleType("lightsei")
    fake.claim_command = MagicMock(return_value=None)
    fake.complete_command = MagicMock(return_value={"id": "cmd-x"})
    fake.send_command = MagicMock(return_value={"id": "cmd-out"})
    fake.emit = MagicMock()
    fake.init = MagicMock()
    monkeypatch.setitem(sys.modules, "lightsei", fake)
    monkeypatch.delitem(sys.modules, "seo_bot", raising=False)
    spec = importlib.util.spec_from_file_location("seo_bot", str(SEO_BOT_PATH))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["seo_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


def _fetcher(text="", *, status_code=200, error=None, site_files=None):
    """A fetch stub. Serves `text` for the page URL; robots.txt / sitemap.xml
    return 404 unless `site_files` maps a path suffix (e.g. "/robots.txt") to
    a response dict."""
    site_files = site_files or {}

    def fetch(url, *, method="GET"):
        for suffix, resp in site_files.items():
            if url.endswith(suffix):
                return {"status_code": 200, "error": None, "text": "", **resp}
        if url.endswith("/robots.txt") or url.endswith("/sitemap.xml"):
            return {"status_code": 404, "error": None, "text": ""}
        return {"status_code": status_code, "error": error, "text": text}
    return fetch


def _fetcher_with_site_files(text):
    """Page + a healthy robots.txt (referencing the sitemap) + sitemap.xml."""
    return _fetcher(text, site_files={
        "/robots.txt": {"text": "User-agent: *\nSitemap: https://s.com/sitemap.xml"},
        "/sitemap.xml": {"text": "<urlset></urlset>"},
    })


def _fake_factory(json_text):
    """An Anthropic-client factory whose messages.create returns json_text
    as a single text block."""
    block = types.SimpleNamespace(type="text", text=json_text)
    usage = types.SimpleNamespace(input_tokens=10, output_tokens=20)
    resp = types.SimpleNamespace(content=[block], usage=usage)
    client = types.SimpleNamespace(
        messages=types.SimpleNamespace(create=lambda **kw: resp))
    return lambda api_key: client


_CLEAN_PAGE = """
<html lang="en"><head>
<title>Acme Plumbing in Austin, TX — Fast 24/7 Service</title>
<meta name="description" content="Acme Plumbing offers fast, affordable 24/7 plumbing service across Austin. Licensed, insured, and trusted by thousands of local homeowners.">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="canonical" href="https://acme.com/">
<meta property="og:title" content="Acme Plumbing">
<script type="application/ld+json">{"@type":"LocalBusiness"}</script>
</head><body>
<h1>Austin's Trusted Plumbers</h1>
<a href="/services">Services</a><a href="/about">About</a><a href="/contact">Contact</a>
<img src="a.jpg" alt="a plumber at work">
<p>%s</p>
</body></html>
""" % ("word " * 320)


_BAD_PAGE = """
<html><head>
<meta name="robots" content="noindex">
</head><body>
<img src="a.jpg">
<p>short</p>
</body></html>
"""


# ---------- audit (pure) ---------- #


def test_audit_clean_page_scores_high(fake_lightsei):
    _, bot = fake_lightsei
    findings = bot.audit_html(_CLEAN_PAGE, "https://acme.com/")
    by = {f["check"]: f for f in findings}
    assert by["title"]["status"] == "good"
    assert by["meta_description"]["status"] == "good"
    assert by["h1"]["status"] == "good"
    assert by["viewport"]["status"] == "good"
    assert by["structured_data"]["status"] == "good"
    assert by["indexable"]["status"] == "good"
    assert bot.score_findings(findings) >= 90


def test_audit_bad_page_flags_issues(fake_lightsei):
    _, bot = fake_lightsei
    findings = bot.audit_html(_BAD_PAGE, "https://x.com/")
    by = {f["check"]: f for f in findings}
    assert by["title"]["status"] == "issue"          # no title
    assert by["meta_description"]["status"] == "issue"  # no description
    assert by["h1"]["status"] == "issue"             # no h1
    assert by["viewport"]["status"] == "issue"       # no viewport
    assert by["indexable"]["status"] == "issue"      # noindex
    assert by["image_alt"]["status"] == "warn"       # img without alt
    assert bot.score_findings(findings) < 60


def test_audit_title_length_warnings(fake_lightsei):
    _, bot = fake_lightsei
    short = bot.audit_html("<title>Hi</title>", "https://x.com/")
    assert next(f for f in short if f["check"] == "title")["status"] == "warn"
    longt = bot.audit_html("<title>" + "x" * 80 + "</title>", "https://x.com/")
    assert next(f for f in longt if f["check"] == "title")["status"] == "warn"


def test_audit_multiple_h1_is_warn(fake_lightsei):
    _, bot = fake_lightsei
    findings = bot.audit_html("<h1>A</h1><h1>B</h1>", "https://x.com/")
    assert next(f for f in findings if f["check"] == "h1")["status"] == "warn"


def test_score_penalizes_issues_more_than_warns(fake_lightsei):
    _, bot = fake_lightsei
    issue = [{"status": "issue"}]
    warn = [{"status": "warn"}]
    assert bot.score_findings(issue) < bot.score_findings(warn)
    assert bot.score_findings([]) == 0


# ---------- audit_site ---------- #


def test_audit_site_reachable(fake_lightsei):
    _, bot = fake_lightsei
    # Healthy page + healthy robots/sitemap = a genuinely clean site.
    r = bot.audit_site("https://acme.com/", _fetcher_with_site_files(_CLEAN_PAGE))
    assert r["reachable"] is True
    assert r["score"] >= 90
    assert r["issues"] == 0
    assert r["severity"] in ("info", "warning")
    checks = {f["check"] for f in r["findings"]}
    assert "robots_txt" in checks and "sitemap" in checks


def test_audit_site_missing_robots_and_sitemap(fake_lightsei):
    _, bot = fake_lightsei
    # Default fetcher 404s robots/sitemap -> a real issue (no sitemap) + warn.
    r = bot.audit_site("https://acme.com/", _fetcher(_CLEAN_PAGE))
    by = {f["check"]: f for f in r["findings"]}
    assert by["robots_txt"]["status"] == "warn"
    assert by["sitemap"]["status"] == "issue"  # no sitemap anywhere


def test_audit_site_files_sitemap_via_robots(fake_lightsei):
    _, bot = fake_lightsei
    # Sitemap only discoverable through robots.txt (no /sitemap.xml 200).
    fetch = _fetcher(_CLEAN_PAGE, site_files={
        "/robots.txt": {"text": "Sitemap: https://acme.com/sm.xml"}})
    findings = bot.audit_site_files("https://acme.com/", fetch)
    by = {f["check"]: f for f in findings}
    assert by["robots_txt"]["status"] == "good"
    assert by["sitemap"]["status"] == "good"  # referenced in robots


def test_audit_site_unreachable(fake_lightsei):
    _, bot = fake_lightsei
    r = bot.audit_site("https://down.com/", _fetcher(error="ConnectTimeout"))
    assert r["reachable"] is False
    assert r["severity"] == "error"
    assert r["findings"] == []


def test_hermes_audit_text_has_score(fake_lightsei):
    _, bot = fake_lightsei
    r = bot.audit_site("https://x.com/", _fetcher(_BAD_PAGE))
    msg = bot.hermes_text_for_audit(r)
    assert "SEO" in msg and "/100" in msg


# ---------- page generation (pure prompt + parse) ---------- #


def test_build_page_prompt_includes_keyword_and_json(fake_lightsei):
    _, bot = fake_lightsei
    system, user = bot.build_page_prompt(
        {"keyword": "emergency plumber austin", "page_type": "service",
         "business_context": "Acme Plumbing"})
    assert "emergency plumber austin" in user
    assert "JSON" in user and "body_html" in user
    assert "service page" in user


def test_parse_page_json_valid(fake_lightsei):
    _, bot = fake_lightsei
    txt = ('Here is your page: {"title":"T","meta_description":"D","slug":"s",'
           '"h1":"H","body_html":"<p>hi</p>"} done')
    page = bot._parse_page_json(txt)
    assert page["title"] == "T" and page["slug"] == "s"


def test_parse_page_json_missing_fields_raises(fake_lightsei):
    _, bot = fake_lightsei
    with pytest.raises(bot.SEOError):
        bot._parse_page_json('{"title":"T"}')


def test_parse_page_json_non_json_raises(fake_lightsei):
    _, bot = fake_lightsei
    with pytest.raises(bot.SEOError):
        bot._parse_page_json("no json here")


def test_generate_page_with_fake_factory(fake_lightsei):
    _, bot = fake_lightsei
    js = ('{"title":"Emergency Plumber Austin","meta_description":"Fast help",'
          '"slug":"emergency-plumber-austin","h1":"Emergency Plumbing in Austin",'
          '"body_html":"<h2>24/7</h2><p>We help.</p>"}')
    out = bot.generate_page({"keyword": "emergency plumber austin"},
                            factory=_fake_factory(js), api_key="sk-test")
    assert out["page"]["slug"] == "emergency-plumber-austin"
    assert out["input_tokens"] == 10 and out["output_tokens"] == 20


# ---------- tick ---------- #


def _cmd(*, cmd_id="cmd-1", kind="seo.audit", payload=None):
    return {"id": cmd_id, "agent_name": "seo", "kind": kind, "payload": payload or {}}


def test_tick_audit_emits_and_completes(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"url": "https://acme.com/"})
    bot.tick(fake, _fetcher(_CLEAN_PAGE))
    assert fake.emit.call_args.args[0] == "seo.audit_complete"
    fake.complete_command.assert_called_once()


def test_tick_audit_bad_page_alerts_hermes(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"url": "https://x.com/"})
    bot.tick(fake, _fetcher(_BAD_PAGE), hermes_channel="alerts")
    fake.send_command.assert_called_once()
    hp = fake.send_command.call_args.args[2]
    assert hp["severity"] == "error" and "/100" in hp["text"]


def test_tick_audit_missing_url_fails(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={})
    bot.tick(fake, _fetcher(""))
    assert "requires a url" in fake.complete_command.call_args.kwargs["error"]


def test_tick_generate_without_key_crashes_cleanly(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake.claim_command.return_value = _cmd(
        kind="seo.generate_page", payload={"keyword": "plumber austin"})
    bot.tick(fake, _fetcher(""))
    assert fake.emit.call_args.args[0] == "seo.crash"
    assert "ANTHROPIC_API_KEY" in fake.complete_command.call_args.kwargs["error"]


def test_tick_generate_page_emits_draft(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    js = ('{"title":"T","meta_description":"D","slug":"plumber-austin",'
          '"h1":"Plumber in Austin","body_html":"<p>hi</p>"}')
    fake.claim_command.return_value = _cmd(
        kind="seo.generate_page", payload={"keyword": "plumber austin"})
    bot.tick(fake, _fetcher(""), factory=_fake_factory(js), hermes_channel="c")
    assert fake.emit.call_args.args[0] == "seo.page_drafted"
    # the page rides the event payload
    assert fake.emit.call_args.args[1]["page"]["slug"] == "plumber-austin"
    fake.send_command.assert_called_once()  # "draft ready" to hermes


def test_tick_unknown_kind_fails(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="seo.dance")
    bot.tick(fake, _fetcher(""))
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake, _fetcher("")) is None
    fake.emit.assert_not_called()


# ---------- SPA / JavaScript-rendered detection ---------- #

_SPA_SHELL = (
    '<html lang="en"><head>'
    '<title>The Restaurant Owners Guide — Tips, Tools and Resources</title>'
    '<meta name="description" content="Everything restaurant owners need to run a better business, in one place for you.">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<link rel="canonical" href="https://x.com/">'
    '<meta property="og:title" content="Guide">'
    '</head><body><div id="root"></div>'
    '<script src="/static/js/react.bundle.js"></script></body></html>'
)


def test_looks_like_spa(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.looks_like_spa(_SPA_SHELL) is True
    # A content-rich server-rendered page is not a SPA even with a root div.
    rich = '<div id="root"></div>' + "<p>" + ("word " * 80) + "</p>"
    assert bot.looks_like_spa(rich) is False


def test_audit_spa_skips_body_checks_and_notes_it(fake_lightsei):
    _, bot = fake_lightsei
    findings = bot.audit_html(_SPA_SHELL, "https://x.com/")
    checks = {f["check"] for f in findings}
    # The honest note is present...
    assert "javascript_rendered" in checks
    # ...and the unreliable body-derived checks are NOT emitted (no false flags).
    assert "h1" not in checks
    assert "content_length" not in checks
    assert "internal_links" not in checks
    assert "image_alt" not in checks
    # Head checks still run + stay accurate.
    assert "title" in checks and "meta_description" in checks
    by = {f["check"]: f for f in findings}
    assert by["meta_description"]["status"] == "good"


def test_audit_server_rendered_runs_body_checks(fake_lightsei):
    _, bot = fake_lightsei
    findings = bot.audit_html(_CLEAN_PAGE, "https://acme.com/")
    checks = {f["check"] for f in findings}
    assert "javascript_rendered" not in checks
    assert "h1" in checks and "content_length" in checks


# ---------- multi-page crawl ---------- #

_PAGE_WITH_LINKS = (
    '<html><head><title>Home Page of Acme Plumbing Co in Austin</title>'
    '<meta name="description" content="Acme Plumbing serves Austin with fast, friendly, licensed service every day.">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<link rel="canonical" href="https://acme.com/"></head><body>'
    '<h1>Acme Plumbing</h1>'
    '<a href="/services">Services</a><a href="/about">About</a>'
    '<a href="https://other.com/x">External</a><a href="/logo.png">img</a>'
    '<p>' + ("word " * 320) + '</p></body></html>'
)


def test_same_origin_links_filters():
    import importlib.util, sys, types, os as _os
    sys.modules.setdefault("lightsei", types.ModuleType("lightsei"))
    p = (Path(__file__).parent.parent.parent / "agents" / "seo" / "bot.py").resolve()
    spec = importlib.util.spec_from_file_location("seo_b2", str(p))
    bot = importlib.util.module_from_spec(spec); spec.loader.exec_module(bot)
    links = bot._same_origin_links(_PAGE_WITH_LINKS, "https://acme.com/", limit=10)
    assert "https://acme.com/services" in links
    assert "https://acme.com/about" in links
    assert "https://other.com/x" not in links     # external dropped
    assert "https://acme.com/logo.png" not in links  # asset dropped
    assert "https://acme.com" not in links         # base page excluded


def test_crawl_site_audits_multiple_pages(fake_lightsei):
    _, bot = fake_lightsei
    # Home links to /services + /about; all reachable + clean-ish.
    fetch = _fetcher(_PAGE_WITH_LINKS, site_files={
        "/robots.txt": {"text": "Sitemap: https://acme.com/sitemap.xml"},
        "/sitemap.xml": {"text": "<urlset/>"},
    })
    r = bot.crawl_site("https://acme.com/", fetch, max_pages=3)
    assert r["pages_audited"] == 3  # home + 2 links
    assert r["average_score"] > 0
    assert len(r["pages"]) == 3
    assert r["start_url"] == "https://acme.com/"
    # top_findings rolls up failing checks across pages
    assert isinstance(r["top_findings"], list)


def test_crawl_respects_max_pages(fake_lightsei):
    _, bot = fake_lightsei
    fetch = _fetcher(_PAGE_WITH_LINKS)
    r = bot.crawl_site("https://acme.com/", fetch, max_pages=1)
    assert r["pages_audited"] == 1  # only the start page


def test_crawl_unreachable_start(fake_lightsei):
    _, bot = fake_lightsei
    fetch = _fetcher(error="ConnectTimeout")
    r = bot.crawl_site("https://down.com/", fetch, max_pages=5)
    assert r["pages_audited"] == 0
    assert r["average_score"] == 0


def test_tick_crawl_emits_complete(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="seo.crawl",
                                           payload={"url": "https://acme.com/", "max_pages": 2})
    bot.tick(fake, _fetcher(_PAGE_WITH_LINKS))
    assert fake.emit.call_args.args[0] == "seo.crawl_complete"
    assert fake.emit.call_args.args[1]["pages_audited"] >= 1
