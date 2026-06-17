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


def _fetcher(text="", *, status_code=200, error=None):
    def fetch(url, *, method="GET"):
        return {"status_code": status_code, "error": error, "text": text}
    return fetch


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
    r = bot.audit_site("https://acme.com/", _fetcher(_CLEAN_PAGE))
    assert r["reachable"] is True
    assert r["score"] >= 90
    assert r["issues"] == 0
    assert r["severity"] in ("info", "warning")


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
