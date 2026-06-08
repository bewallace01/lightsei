"""Website assistant — keeps a small business's site healthy.

Part of the "AI Business Team" roster (Phase 32.1). The Website
assistant watches a business's website the way a diligent employee would:
is the site up? are there broken links a customer might hit? is the
contact / lead-capture form still on the page? It polls its command
queue for `website.check` commands, runs the checks, emits a
`website.check_complete` event, and (only when something is wrong)
dispatches a `hermes.post` alert so the owner hears about it.

No LLM, no connectors — pure HTTP checks. Same bot contract as the rest
of the constellation (claim -> check -> emit -> maybe-dispatch ->
complete), so it deploys on the worker like any other.

Phase 32.1 scope: one command kind (`website.check`), one downstream
dispatch (`hermes.post`, only on a down site or broken links), two event
types (`website.check_complete` + `website.crash`).

Env (defaults in parens):
  WEBSITE_POLL_S         seconds between claim attempts (5)
  WEBSITE_HERMES_CHANNEL channel passed to Hermes (default)
  WEBSITE_MAX_LINKS      cap on links probed per check (25)
  WEBSITE_TIMEOUT_S      per-request timeout (10)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required.

Public surface (for tests):
  extract_links(html, base_url) -> [absolute url]
  extract_forms(html) -> [{action, method}]
  classify_status(status_code, error) -> {up, severity}
  check_site(url, fetch, *, max_links) -> report dict
  tick(client, fetch, *, hermes_channel=...)
  main()
"""
import os
import re
import sys
import time
import traceback
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

import lightsei


def _send_with_source(target_agent, kind, payload, *, source_agent):
    try:
        return lightsei.send_command(target_agent, kind, payload, source_agent=source_agent)
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("WEBSITE_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("WEBSITE_HERMES_CHANNEL", "default")
MAX_LINKS = int(os.environ.get("WEBSITE_MAX_LINKS", "25"))
TIMEOUT_S = float(os.environ.get("WEBSITE_TIMEOUT_S", "10"))


# ---------- Pure helpers ---------- #

_HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)
_FORM_RE = re.compile(r"""<form\b([^>]*)>""", re.IGNORECASE)
_ATTR_RE = re.compile(r"""(\w+)\s*=\s*["']([^"']*)["']""")


def extract_links(html: str, base_url: str) -> list[str]:
    """Absolute http(s) links from the page, deduped, in first-seen order.
    Skips anchors, mailto/tel/javascript, and off-page schemes."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _HREF_RE.findall(html or ""):
        raw = raw.strip()
        if not raw or raw.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, raw)
        if urlparse(absolute).scheme not in ("http", "https"):
            continue
        if absolute not in seen:
            seen.add(absolute)
            out.append(absolute)
    return out


def extract_forms(html: str) -> list[dict[str, Any]]:
    """Forms on the page (lead-capture / contact). Returns action+method."""
    forms: list[dict[str, Any]] = []
    for attrs in _FORM_RE.findall(html or ""):
        a = {k.lower(): v for k, v in _ATTR_RE.findall(attrs)}
        forms.append({"action": a.get("action", ""), "method": (a.get("method") or "get").lower()})
    return forms


def classify_status(status_code: Optional[int], error: Optional[str]) -> dict[str, Any]:
    """A page is 'up' on a 2xx/3xx with no transport error."""
    if error:
        return {"up": False, "severity": "error"}
    if status_code is not None and 200 <= status_code < 400:
        return {"up": True, "severity": "info"}
    return {"up": False, "severity": "error"}


def is_broken_link(status_code: Optional[int], error: Optional[str]) -> bool:
    """Whether a link a customer would click is genuinely broken.

    Conservative on purpose: only a transport error, a gone resource
    (404/410), or a server error (5xx) counts. 401/403/405 mean the
    resource exists but is auth-gated or method-restricted (e.g. a server
    that rejects HEAD) — a browser GET would still load it, so flagging
    those would spam the owner with false alarms.
    """
    if error:
        return True
    if status_code is None:
        return True
    return status_code in (404, 410) or status_code >= 500


# Fetcher DI seam. Production passes an httpx-backed one; tests stub it.
# Returns {status_code: int|None, error: str|None, text: str, latency_ms: int}.
Fetcher = Callable[..., dict[str, Any]]


def check_site(url: str, fetch: Fetcher, *, max_links: int = 25) -> dict[str, Any]:
    """Core check. GETs the page (uptime + html), probes up to `max_links`
    links (HEAD), and detects forms. Pure given an injected `fetch`."""
    page = fetch(url, method="GET")
    status = classify_status(page.get("status_code"), page.get("error"))
    report: dict[str, Any] = {
        "url": url,
        "up": status["up"],
        "status_code": page.get("status_code"),
        "latency_ms": page.get("latency_ms"),
        "broken_links": [],
        "forms_found": 0,
        "links_checked": 0,
    }
    if not status["up"]:
        report["severity"] = "error"
        report["error"] = page.get("error")
        return report

    html = page.get("text") or ""
    report["forms_found"] = len(extract_forms(html))

    links = extract_links(html, url)[:max_links]
    broken: list[dict[str, Any]] = []
    for link in links:
        # GET (like a real browser) rather than HEAD: many servers reject
        # HEAD with 405, which would be a false "broken" alert.
        res = fetch(link, method="GET")
        if is_broken_link(res.get("status_code"), res.get("error")):
            broken.append({"url": link, "status": res.get("status_code"), "error": res.get("error")})
    report["links_checked"] = len(links)
    report["broken_links"] = broken
    report["severity"] = "error" if broken else "info"
    return report


def hermes_text_for(report: dict[str, Any]) -> str:
    """One-line owner-facing alert. Only called when something's wrong."""
    if not report["up"]:
        return f"\U0001f534 website: {report['url']} appears DOWN (status {report.get('status_code')})"
    n = len(report["broken_links"])
    return f"⚠️ website: {n} broken link{'s' if n != 1 else ''} on {report['url']}"


# ---------- Production fetcher ---------- #


def _httpx_fetch(url: str, *, method: str = "GET") -> dict[str, Any]:
    import httpx
    started = time.monotonic()
    try:
        resp = httpx.request(
            method, url, timeout=TIMEOUT_S, follow_redirects=True,
            headers={"User-Agent": "Lightsei-Website-Assistant/1.0"},
        )
        return {
            "status_code": resp.status_code,
            "error": None,
            "text": resp.text if method == "GET" else "",
            "latency_ms": int((time.monotonic() - started) * 1000),
        }
    except Exception as e:
        return {"status_code": None, "error": f"{type(e).__name__}: {e}", "text": "",
                "latency_ms": int((time.monotonic() - started) * 1000)}


# ---------- Bot loop ---------- #


def tick(client: Any, fetch: Fetcher = _httpx_fetch, *, hermes_channel: str = "default") -> Optional[dict[str, Any]]:
    cmd = lightsei.claim_command(agent_name="website")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "website.check":
        lightsei.complete_command(cmd_id, error=f"website does not handle kind={kind!r}")
        return cmd

    payload = cmd.get("payload") or {}
    url = payload.get("url") or ""
    if not url:
        lightsei.complete_command(cmd_id, error="website.check requires a url")
        return cmd

    try:
        report = check_site(url, fetch, max_links=MAX_LINKS)
    except Exception as e:
        lightsei.emit("website.crash", {"command_id": cmd_id, "error": repr(e),
                                        "traceback": traceback.format_exc()})
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"⚠️ website: crashed checking {url} ({type(e).__name__})",
                               "severity": "error"}, source_agent="website")
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    report["command_id"] = cmd_id
    lightsei.emit("website.check_complete", report)

    # Only wake the owner when something is actually wrong.
    if not report["up"] or report["broken_links"]:
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel, "text": hermes_text_for(report), "severity": "error"},
                              source_agent="website")
        except Exception as e:
            print(f"website: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=report)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "website")
    if not api_key:
        print("website: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"website up: agent={agent_name} channel={HERMES_CHANNEL} max_links={MAX_LINKS}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"website tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
