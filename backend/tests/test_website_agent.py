"""Phase 32.1: Website assistant.

Drives `agents/website/bot.py`'s pure checks + tick in isolation with a
stubbed `lightsei` module and an injected fake fetcher.
"""
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest


WEBSITE_BOT_PATH = (
    Path(__file__).parent.parent.parent / "agents" / "website" / "bot.py"
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
    monkeypatch.delitem(sys.modules, "website_bot", raising=False)
    spec = importlib.util.spec_from_file_location("website_bot", str(WEBSITE_BOT_PATH))
    bot = importlib.util.module_from_spec(spec)
    sys.modules["website_bot"] = bot
    spec.loader.exec_module(bot)
    return fake, bot


def _fetcher(status_map):
    """status_map: url -> {status_code|error|text}. Default 200 + empty."""
    def fetch(url, *, method="GET"):
        entry = dict(status_map.get(url, {"status_code": 200, "text": ""}))
        entry.setdefault("status_code", 200)
        entry.setdefault("error", None)
        entry.setdefault("text", "")
        entry.setdefault("latency_ms", 12)
        return entry
    return fetch


# ---------- pure helpers ---------- #


def test_extract_links_absolute_dedup_and_skip(fake_lightsei):
    _, bot = fake_lightsei
    html = """
      <a href="/about">a</a> <a href="https://x.com/p">b</a>
      <a href="/about">dup</a> <a href="mailto:x@y.com">m</a>
      <a href="#top">anchor</a> <a href="tel:123">t</a>
    """
    links = bot.extract_links(html, "https://site.com/")
    assert links == ["https://site.com/about", "https://x.com/p"]


def test_extract_forms(fake_lightsei):
    _, bot = fake_lightsei
    html = '<form action="/lead" method="POST">..</form><form>..</form>'
    forms = bot.extract_forms(html)
    assert forms[0] == {"action": "/lead", "method": "post"}
    assert forms[1]["method"] == "get"  # default


def test_classify_status(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.classify_status(200, None)["up"] is True
    assert bot.classify_status(301, None)["up"] is True
    assert bot.classify_status(404, None)["up"] is False
    assert bot.classify_status(500, None)["up"] is False
    assert bot.classify_status(None, "Timeout")["up"] is False


def test_is_broken_link_is_conservative(fake_lightsei):
    _, bot = fake_lightsei
    # Genuinely broken: gone, server error, transport error.
    assert bot.is_broken_link(404, None) is True
    assert bot.is_broken_link(410, None) is True
    assert bot.is_broken_link(503, None) is True
    assert bot.is_broken_link(None, "Timeout") is True
    # NOT broken: a browser GET would still load these.
    assert bot.is_broken_link(200, None) is False
    assert bot.is_broken_link(301, None) is False
    assert bot.is_broken_link(403, None) is False  # auth-gated
    assert bot.is_broken_link(405, None) is False  # method-restricted (HEAD reject)
    assert bot.is_broken_link(401, None) is False


def test_check_site_does_not_flag_405_or_403(fake_lightsei):
    _, bot = fake_lightsei
    html = '<a href="/auth">a</a><a href="/head-reject">b</a>'
    fetch = _fetcher({
        "https://s.com/": {"status_code": 200, "text": html},
        "https://s.com/auth": {"status_code": 403},
        "https://s.com/head-reject": {"status_code": 405},
    })
    r = bot.check_site("https://s.com/", fetch)
    assert r["broken_links"] == []
    assert r["severity"] == "info"


# ---------- check_site ---------- #


def test_check_site_down(fake_lightsei):
    _, bot = fake_lightsei
    fetch = _fetcher({"https://down.com": {"status_code": 503}})
    r = bot.check_site("https://down.com", fetch)
    assert r["up"] is False and r["severity"] == "error"
    assert r["links_checked"] == 0  # didn't bother probing links on a down page


def test_check_site_broken_link(fake_lightsei):
    _, bot = fake_lightsei
    html = '<a href="/ok">ok</a><a href="/gone">gone</a><form action="/lead"></form>'
    fetch = _fetcher({
        "https://s.com/": {"status_code": 200, "text": html},
        "https://s.com/ok": {"status_code": 200},
        "https://s.com/gone": {"status_code": 404},
    })
    r = bot.check_site("https://s.com/", fetch)
    assert r["up"] is True
    assert r["forms_found"] == 1
    assert r["links_checked"] == 2
    assert [b["url"] for b in r["broken_links"]] == ["https://s.com/gone"]
    assert r["severity"] == "error"


def test_check_site_healthy(fake_lightsei):
    _, bot = fake_lightsei
    html = '<a href="/ok">ok</a>'
    fetch = _fetcher({"https://s.com/": {"status_code": 200, "text": html},
                      "https://s.com/ok": {"status_code": 200}})
    r = bot.check_site("https://s.com/", fetch)
    assert r["severity"] == "info" and r["broken_links"] == []


def test_check_site_respects_max_links(fake_lightsei):
    _, bot = fake_lightsei
    html = "".join(f'<a href="/p{i}">x</a>' for i in range(50))
    fetch = _fetcher({"https://s.com/": {"status_code": 200, "text": html}})
    r = bot.check_site("https://s.com/", fetch, max_links=10)
    assert r["links_checked"] == 10


# ---------- tick ---------- #


def _cmd(*, cmd_id="cmd-1", kind="website.check", payload=None):
    return {"id": cmd_id, "agent_name": "website", "kind": kind, "payload": payload or {}}


def test_tick_down_site_alerts_hermes(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"url": "https://down.com"})
    # A genuine outage: the server won't accept a connection (not a 500,
    # which means the server is up but erroring — that path is covered
    # separately). An unreachable site is the one that reads "DOWN".
    fetch = _fetcher({"https://down.com": {
        "status_code": None, "error": "ConnectError: [Errno 61] Connection refused"}})
    bot.tick(fake, fetch, hermes_channel="alerts")
    fake.emit.assert_called_once()
    assert fake.emit.call_args.args[0] == "website.check_complete"
    fake.send_command.assert_called_once()
    target, kind, hp = fake.send_command.call_args.args[:3]
    assert target == "hermes" and hp["severity"] == "error" and "DOWN" in hp["text"]
    assert fake.send_command.call_args.kwargs["source_agent"] == "website"


def test_tick_healthy_site_no_alert(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"url": "https://s.com/"})
    fetch = _fetcher({"https://s.com/": {"status_code": 200, "text": "<a href='/ok'>x</a>"},
                      "https://s.com/ok": {"status_code": 200}})
    bot.tick(fake, fetch)
    fake.emit.assert_called_once()
    fake.send_command.assert_not_called()


def test_tick_missing_url_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={})
    bot.tick(fake, _fetcher({}))
    fake.emit.assert_not_called()
    assert "requires a url" in fake.complete_command.call_args.kwargs["error"]


def test_tick_unknown_kind_completes_failed(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(kind="website.dance")
    bot.tick(fake, _fetcher({}))
    assert "does not handle" in fake.complete_command.call_args.kwargs["error"]


def test_tick_empty_queue_returns_none(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = None
    assert bot.tick(fake, _fetcher({})) is None
    fake.emit.assert_not_called()


def test_tick_crash_emits_website_crash(fake_lightsei, monkeypatch):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"url": "https://s.com/"})
    monkeypatch.setattr(bot, "check_site", MagicMock(side_effect=RuntimeError("boom")))
    bot.tick(fake, _fetcher({}))
    assert fake.emit.call_args.args[0] == "website.crash"
    fake.send_command.assert_called_once()
    assert "error" in fake.complete_command.call_args.kwargs


# ---------- TLS / cert vs down distinction (hardening) ---------- #


def test_classify_error_detects_tls(fake_lightsei):
    _, bot = fake_lightsei
    for msg in (
        "SSLError: certificate verify failed",
        "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
        "ssl.SSLCertVerificationError: hostname mismatch",
    ):
        assert bot._classify_error(msg) == "tls", msg
    # Non-TLS transport failures are 'unreachable'.
    for msg in ("ConnectTimeout: timed out", "ConnectError: [Errno 61] Connection refused",
                "ReadTimeout"):
        assert bot._classify_error(msg) == "unreachable", msg


def test_classify_status_carries_reason(fake_lightsei):
    _, bot = fake_lightsei
    assert bot.classify_status(200, None)["reason"] == "ok"
    assert bot.classify_status(404, None)["reason"] == "http_error"
    assert bot.classify_status(500, None)["reason"] == "http_error"
    assert bot.classify_status(None, "SSLError: certificate verify failed")["reason"] == "tls"
    assert bot.classify_status(None, "ConnectTimeout")["reason"] == "unreachable"
    # Backward-compatible: up/severity still present.
    s = bot.classify_status(None, "SSLError: bad cert")
    assert s["up"] is False and s["severity"] == "error"


def test_check_site_tls_error_carries_reason(fake_lightsei):
    _, bot = fake_lightsei
    fetch = _fetcher({"https://www.x.com": {
        "status_code": None,
        "error": "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
    }})
    r = bot.check_site("https://www.x.com", fetch)
    assert r["up"] is False
    assert r["reason"] == "tls"
    assert r["links_checked"] == 0  # didn't probe links on a failed page


def test_hermes_text_distinguishes_tls_from_down(fake_lightsei):
    _, bot = fake_lightsei
    tls = bot.hermes_text_for(
        {"up": False, "reason": "tls", "url": "https://www.x.com", "status_code": None,
         "broken_links": []})
    assert "certificate" in tls and "DOWN" not in tls
    down = bot.hermes_text_for(
        {"up": False, "reason": "unreachable", "url": "https://x.com", "status_code": None,
         "broken_links": []})
    assert "DOWN" in down
    http = bot.hermes_text_for(
        {"up": False, "reason": "http_error", "url": "https://x.com", "status_code": 503,
         "broken_links": []})
    assert "error" in http and "503" in http


def test_tick_tls_error_alerts_with_cert_message_not_down(fake_lightsei):
    fake, bot = fake_lightsei
    fake.claim_command.return_value = _cmd(payload={"url": "https://www.x.com"})
    fetch = _fetcher({"https://www.x.com": {
        "status_code": None,
        "error": "ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
    }})
    bot.tick(fake, fetch, hermes_channel="alerts")
    # Emits the result event and alerts hermes with the accurate cert wording.
    assert fake.emit.call_args.args[0] == "website.check_complete"
    hp = fake.send_command.call_args.args[2]
    assert hp["severity"] == "error"
    assert "certificate" in hp["text"] and "DOWN" not in hp["text"]
