"""SEO assistant — the AI Business Team's search-optimization specialist.

Part of the roster. Two jobs:

 1. **Audit** (heuristic, no LLM): crawl a page and score its on-page SEO —
    title tag, meta description, a single H1, image alt coverage, canonical
    tag, viewport (mobile), structured data, Open Graph tags, word count,
    indexability — emitting a prioritized list of findings + a 0-100 score.
    Runs on a feeder so the assistant is always working on SEO.

 2. **Generate a new page** (LLM): given a target keyword + the business,
    write a complete SEO-optimized page (title, meta description, slug, H1,
    body HTML) as a draft, ready for the owner to review and (Phase 2)
    publish to their CMS.

Command kinds: `seo.audit`, `seo.generate_page`.
Events: `seo.audit_complete`, `seo.crawl_complete`, `seo.page_drafted`,
`seo.suggestions`, `seo.crash`.
Downstream: one `hermes.post` (audit found issues / a draft is ready).

The audit half needs no API key (pure HTTP + parsing). The generate half is
LLM-backed and needs the workspace's ANTHROPIC_API_KEY (injected by the
worker); it fails cleanly with a clear message when absent.

Env (defaults in parens):
  SEO_POLL_S          seconds between claim attempts (5)
  SEO_HERMES_CHANNEL  channel passed to Hermes (default)
  SEO_TIMEOUT_S       per-request timeout (10)
  SEO_MODEL           Claude model (claude-sonnet-4-6)
  SEO_MAX_TOKENS      output cap for page generation (1500)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY    required (bot auth).
  ANTHROPIC_API_KEY   required for seo.generate_page only.

Public surface (for tests):
  audit_html(html, url) -> list[finding]
  score_findings(findings) -> int
  audit_site(url, fetch) -> report dict
  build_page_prompt(payload) -> (system, user)
  generate_page(payload, *, factory, api_key, model, max_tokens) -> dict
  tick(client, fetch=..., *, factory=..., hermes_channel=..., ...)
  main()
"""
import json
import os
import re
import sys
import time
import traceback
import uuid
from typing import Any, Callable, Optional
from urllib.parse import urljoin, urlparse

import lightsei


def _send_with_source(target_agent, kind, payload, *, source_agent):
    try:
        return lightsei.send_command(target_agent, kind, payload, source_agent=source_agent)
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("SEO_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("SEO_HERMES_CHANNEL", "default")
TIMEOUT_S = float(os.environ.get("SEO_TIMEOUT_S", "10"))
MODEL = os.environ.get("SEO_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("SEO_MAX_TOKENS", "1500"))


# ---------- Pure parsing helpers ---------- #

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(r"<meta\b([^>]*)>", re.IGNORECASE)
_LINK_RE = re.compile(r"<link\b([^>]*)>", re.IGNORECASE)
_ATTR_RE = re.compile(r"""([\w:-]+)\s*=\s*["']([^"']*)["']""")
_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_IMG_RE = re.compile(r"<img\b([^>]*)>", re.IGNORECASE)
_HREF_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)
_JSONLD_RE = re.compile(
    r"""<script\b[^>]*type\s*=\s*["']application/ld\+json["']""", re.IGNORECASE
)
_HTMLLANG_RE = re.compile(r"<html\b[^>]*\blang\s*=", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(
    r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL
)
# An empty mount point a JS framework hydrates into (React/Vue/Next/etc.).
_SPA_ROOT_RE = re.compile(r"""\bid\s*=\s*["'](?:root|app|__next|__nuxt)["']""", re.IGNORECASE)
_SPA_LIB_RE = re.compile(r"\b(react|vue|angular|svelte|nuxt|/_next/)\b", re.IGNORECASE)


def looks_like_spa(html: str) -> bool:
    """Heuristic: the page ships almost no rendered content but loads a JS
    framework — i.e. the body is client-rendered, so Spica (which reads raw
    HTML, no JS) can't see the real H1 / content / links. The low-word gate
    keeps content-rich server-rendered pages from tripping it."""
    if _visible_word_count(html) >= 50:
        return False
    h = html or ""
    return bool(_SPA_ROOT_RE.search(h)) or bool(_SPA_LIB_RE.search(h))


def _attrs(s: str) -> dict[str, str]:
    return {k.lower(): v for k, v in _ATTR_RE.findall(s or "")}


def _metas(html: str) -> list[dict[str, str]]:
    return [_attrs(m) for m in _META_RE.findall(html or "")]


def _meta_content(html: str, *, name: Optional[str] = None,
                  prop: Optional[str] = None) -> Optional[str]:
    """The content of the first <meta> matching name= or property= (order-
    independent, since attribute order varies in real pages)."""
    for a in _metas(html):
        if name is not None and a.get("name", "").lower() == name.lower():
            return a.get("content", "")
        if prop is not None and a.get("property", "").lower() == prop.lower():
            return a.get("content", "")
    return None


def _has_canonical(html: str) -> bool:
    for link in _LINK_RE.findall(html or ""):
        if _attrs(link).get("rel", "").lower() == "canonical":
            return True
    return False


def _visible_word_count(html: str) -> int:
    stripped = _SCRIPT_STYLE_RE.sub(" ", html or "")
    text = _TAG_RE.sub(" ", stripped)
    return len([w for w in text.split() if w.strip()])


def _is_noindex(html: str) -> bool:
    robots = _meta_content(html, name="robots") or ""
    return "noindex" in robots.lower()


def _internal_link_count(html: str, base_url: str) -> int:
    host = urlparse(base_url).netloc
    n = 0
    for raw in _HREF_RE.findall(html or ""):
        raw = raw.strip()
        if not raw or raw.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, raw)
        p = urlparse(absolute)
        if p.scheme in ("http", "https") and p.netloc == host:
            n += 1
    return n


# ---------- Audit (heuristic, pure) ---------- #


def _finding(check: str, status: str, detail: str, recommendation: str) -> dict[str, Any]:
    return {"check": check, "status": status, "detail": detail,
            "recommendation": recommendation}


def audit_html(html: str, url: str) -> list[dict[str, Any]]:
    """Run the on-page SEO checks over a page's HTML. Pure + testable.

    Each finding is {check, status, detail, recommendation} where status is
    'good' (no action), 'warn' (worth improving), or 'issue' (likely hurting
    rankings). Conservative: only flags things a real audit tool would.
    """
    html = html or ""
    out: list[dict[str, Any]] = []

    # Title tag (aim 30-60 chars).
    tm = _TITLE_RE.search(html)
    title = (tm.group(1).strip() if tm else "")
    if not title:
        out.append(_finding("title", "issue", "No <title> tag.",
                            "Add a unique, descriptive title of 30-60 characters."))
    elif len(title) < 30:
        out.append(_finding("title", "warn", f"Title is short ({len(title)} chars).",
                            "Aim for 30-60 characters with your main keyword."))
    elif len(title) > 60:
        out.append(_finding("title", "warn", f"Title is long ({len(title)} chars); may be truncated.",
                            "Trim the title to about 60 characters."))
    else:
        out.append(_finding("title", "good", f"Title present ({len(title)} chars).", ""))

    # Meta description (aim 50-160 chars).
    desc = _meta_content(html, name="description")
    if desc is None:
        out.append(_finding("meta_description", "issue", "No meta description.",
                            "Add a 50-160 character summary that invites the click."))
    elif len(desc) < 50:
        out.append(_finding("meta_description", "warn", f"Meta description is short ({len(desc)} chars).",
                            "Expand it to 50-160 characters."))
    elif len(desc) > 160:
        out.append(_finding("meta_description", "warn", f"Meta description is long ({len(desc)} chars).",
                            "Trim it to about 160 characters so it isn't cut off."))
    else:
        out.append(_finding("meta_description", "good", f"Meta description present ({len(desc)} chars).", ""))

    # If the page is a JavaScript-rendered shell, Spica only sees the empty
    # mount HTML, so the body-derived checks (H1, content, links, images)
    # would false-flag. Note it once and skip those; the <head> checks are
    # still accurate.
    spa = looks_like_spa(html)
    if spa:
        out.append(_finding(
            "javascript_rendered", "warn",
            "Page renders its content with JavaScript; Spica reads the initial "
            "HTML, so content checks (H1, length, links, images) can't be "
            "verified from here.",
            "Use server-side rendering or prerendering so search tools (and "
            "some crawlers) see your content directly."))

    # Exactly one H1. (Body-rendered: skipped on JS-shell pages.)
    if not spa:
        h1s = [h.strip() for h in _H1_RE.findall(html)]
        if len(h1s) == 0:
            out.append(_finding("h1", "issue", "No <h1> heading.",
                                "Add one clear H1 describing the page's topic."))
        elif len(h1s) > 1:
            out.append(_finding("h1", "warn", f"{len(h1s)} H1 headings.",
                                "Use a single H1; demote the rest to H2/H3."))
        else:
            out.append(_finding("h1", "good", "One H1 heading.", ""))

    # Image alt coverage. (Body-rendered: skipped on JS-shell pages.)
    if not spa:
        imgs = _IMG_RE.findall(html)
        if imgs:
            missing = sum(1 for i in imgs if not _attrs(i).get("alt", "").strip())
            if missing:
                out.append(_finding("image_alt", "warn",
                                    f"{missing} of {len(imgs)} images missing alt text.",
                                    "Add descriptive alt text to every meaningful image."))
            else:
                out.append(_finding("image_alt", "good", f"All {len(imgs)} images have alt text.", ""))

    # Canonical tag.
    if not _has_canonical(html):
        out.append(_finding("canonical", "warn", "No canonical link tag.",
                            "Add <link rel=\"canonical\"> to avoid duplicate-content issues."))
    else:
        out.append(_finding("canonical", "good", "Canonical tag present.", ""))

    # Mobile viewport.
    if _meta_content(html, name="viewport") is None:
        out.append(_finding("viewport", "issue", "No mobile viewport meta tag.",
                            "Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">."))
    else:
        out.append(_finding("viewport", "good", "Mobile viewport set.", ""))

    # Structured data (JSON-LD).
    if not _JSONLD_RE.search(html):
        out.append(_finding("structured_data", "warn", "No JSON-LD structured data.",
                            "Add schema.org markup (e.g. LocalBusiness) so search engines understand the page."))
    else:
        out.append(_finding("structured_data", "good", "Structured data present.", ""))

    # Open Graph (social sharing).
    if _meta_content(html, prop="og:title") is None:
        out.append(_finding("open_graph", "warn", "No Open Graph tags.",
                            "Add og:title and og:description so shared links look good."))
    else:
        out.append(_finding("open_graph", "good", "Open Graph tags present.", ""))

    # Indexability.
    if _is_noindex(html):
        out.append(_finding("indexable", "issue", "Page is set to noindex.",
                            "Remove the noindex robots tag if you want this page in search."))
    else:
        out.append(_finding("indexable", "good", "Page is indexable.", ""))

    # Thin content. (Body-rendered: skipped on JS-shell pages.)
    if not spa:
        words = _visible_word_count(html)
        if words < 300:
            out.append(_finding("content_length", "warn", f"Thin content ({words} words).",
                                "Aim for 300+ words of useful, original content."))
        else:
            out.append(_finding("content_length", "good", f"Content length OK ({words} words).", ""))

    # Internal linking. (Body-rendered: skipped on JS-shell pages.)
    if not spa:
        links = _internal_link_count(html, url)
        if links < 3:
            out.append(_finding("internal_links", "warn", f"Few internal links ({links}).",
                                "Link to related pages to spread authority and help navigation."))
        else:
            out.append(_finding("internal_links", "good", f"Internal links OK ({links}).", ""))

    return out


def score_findings(findings: list[dict[str, Any]]) -> int:
    """A 0-100 on-page SEO score. Each issue costs more than a warn; a clean
    page scores 100."""
    if not findings:
        return 0
    penalty = 0
    for f in findings:
        if f["status"] == "issue":
            penalty += 12
        elif f["status"] == "warn":
            penalty += 5
    return max(0, 100 - penalty)


def _origin(url: str) -> str:
    """scheme://host[:port] for a URL (where robots.txt / sitemap.xml live)."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def _ok(resp: dict[str, Any]) -> bool:
    code = resp.get("status_code")
    return (not resp.get("error")) and code is not None and 200 <= code < 400


def audit_site_files(url: str, fetch: Callable[..., dict[str, Any]]) -> list[dict[str, Any]]:
    """Site-level crawlability checks: robots.txt + sitemap.xml. These govern
    whether search engines can crawl + discover the site, so a missing one is
    a real ranking problem distinct from on-page issues. Pure given `fetch`.

    Looks for the sitemap either at /sitemap.xml or referenced in robots.txt.
    """
    origin = _origin(url)
    out: list[dict[str, Any]] = []

    robots = fetch(f"{origin}/robots.txt", method="GET")
    robots_text = (robots.get("text") or "") if _ok(robots) else ""
    if not _ok(robots):
        out.append(_finding("robots_txt", "warn", "No robots.txt found.",
                            "Add a robots.txt so you can guide crawlers and point them to your sitemap."))
    else:
        out.append(_finding("robots_txt", "good", "robots.txt present.", ""))

    sitemap_in_robots = "sitemap:" in robots_text.lower()
    sm = fetch(f"{origin}/sitemap.xml", method="GET")
    has_sitemap = _ok(sm) or sitemap_in_robots
    if not has_sitemap:
        out.append(_finding("sitemap", "issue", "No sitemap.xml found.",
                            "Add a sitemap.xml (and reference it in robots.txt) so search engines discover all your pages."))
    elif not sitemap_in_robots:
        out.append(_finding("sitemap", "warn", "Sitemap exists but isn't referenced in robots.txt.",
                            "Add a 'Sitemap:' line to robots.txt so crawlers find it reliably."))
    else:
        out.append(_finding("sitemap", "good", "Sitemap present and referenced in robots.txt.", ""))

    return out


def audit_site(url: str, fetch: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """Fetch a page and audit it (on-page + site-level crawlability). Pure
    given an injected `fetch` (same seam the website assistant uses)."""
    page = fetch(url, method="GET")
    err = page.get("error")
    code = page.get("status_code")
    up = (not err) and (code is not None and 200 <= code < 400)
    report: dict[str, Any] = {
        "url": url,
        "reachable": up,
        "status_code": code,
        "findings": [],
        "score": 0,
        "issues": 0,
        "warnings": 0,
    }
    if not up:
        report["severity"] = "error"
        report["error"] = err or f"status {code}"
        return report

    findings = audit_html(page.get("text") or "", url)
    # Site-level crawlability (robots.txt + sitemap.xml) — best-effort; a
    # fetch failure on these never sinks the whole audit.
    try:
        findings = findings + audit_site_files(url, fetch)
    except Exception:
        pass
    issues = sum(1 for f in findings if f["status"] == "issue")
    warns = sum(1 for f in findings if f["status"] == "warn")
    report["findings"] = findings
    report["score"] = score_findings(findings)
    report["issues"] = issues
    report["warnings"] = warns
    report["severity"] = "error" if issues else ("warning" if warns else "info")
    return report


def hermes_text_for_audit(report: dict[str, Any]) -> str:
    if not report["reachable"]:
        return f"\U0001f534 SEO: couldn't reach {report['url']} to audit it."
    i, w, s = report["issues"], report["warnings"], report["score"]
    return (f"\U0001f50d SEO: {report['url']} scored {s}/100 — "
            f"{i} issue{'s' if i != 1 else ''}, {w} improvement{'s' if w != 1 else ''} to make.")


def _same_origin_links(html: str, base_url: str, *, limit: int) -> list[str]:
    """Same-origin page links from the HTML, deduped, first-seen order,
    skipping the base page + obvious non-pages (assets, anchors)."""
    if limit <= 0:
        return []
    base = base_url.rstrip("/")
    host = urlparse(base_url).netloc
    out: list[str] = []
    seen = {base}
    _ASSET = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".css", ".js",
              ".pdf", ".zip", ".ico", ".xml", ".json")
    for raw in _HREF_RE.findall(html or ""):
        raw = raw.strip()
        if not raw or raw.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute = urljoin(base_url, raw).split("#")[0].rstrip("/")
        p = urlparse(absolute)
        if p.scheme not in ("http", "https") or p.netloc != host:
            continue
        if absolute.lower().endswith(_ASSET):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append(absolute)
        if len(out) >= limit:
            break
    return out


def crawl_site(url: str, fetch: Callable[..., dict[str, Any]], *,
               max_pages: int = 5) -> dict[str, Any]:
    """Audit up to `max_pages` pages: the start URL plus the same-origin
    pages it links to. Pure given `fetch`. Returns a site rollup:
    {start_url, pages_audited, average_score, lowest_score, pages: [...],
     top_findings}. Each page entry is {url, score, issues, warnings}.
    """
    start = audit_site(url, fetch)
    pages = [start]
    if start.get("reachable"):
        # Discover links from the start page's HTML (re-fetch is cheap; the
        # start audit didn't keep the raw HTML).
        page = fetch(url, method="GET")
        links = _same_origin_links(page.get("text") or "", url,
                                   limit=max(0, max_pages - 1))
        for link in links:
            pages.append(audit_site(link, fetch))

    reachable = [p for p in pages if p.get("reachable")]
    scores = [p["score"] for p in reachable]
    avg = round(sum(scores) / len(scores)) if scores else 0

    # Roll up which checks fail most across the crawl.
    from collections import Counter
    counter: Counter = Counter()
    for p in reachable:
        for f in p.get("findings", []):
            if f["status"] != "good":
                counter[f["check"]] += 1
    top = [{"check": c, "pages": n} for c, n in counter.most_common(5)]

    return {
        "start_url": url,
        "pages_audited": len(reachable),
        "average_score": avg,
        "lowest_score": min(scores) if scores else 0,
        "pages": [
            {"url": p["url"], "score": p.get("score", 0),
             "issues": p.get("issues", 0), "warnings": p.get("warnings", 0),
             "reachable": p.get("reachable", False)}
            for p in pages
        ],
        "top_findings": top,
        "severity": "error" if any(p.get("issues") for p in reachable) else (
            "warning" if any(p.get("warnings") for p in reachable) else "info"),
    }


def hermes_text_for_crawl(report: dict[str, Any]) -> str:
    n = report["pages_audited"]
    return (f"\U0001f50d SEO: audited {n} page{'s' if n != 1 else ''} — "
            f"average score {report['average_score']}/100 "
            f"(lowest {report['lowest_score']}).")


# ---------- Page generation (LLM) ---------- #

ClientFactory = Callable[[str], Any]

_PAGE_TYPES = ("service", "location", "blog", "landing")

_GEN_SYSTEM = (
    "You are an SEO content specialist on a small business's team. You write "
    "complete, genuinely useful web pages optimized for a target keyword, in "
    "the business's voice: clear, concrete, no fluff, no keyword stuffing, no "
    "em dashes. You return ONLY a single JSON object, no prose around it."
)

_INDUSTRY_LABELS = {
    "restaurant": "restaurant or cafe",
    "home_services": "home services business",
    "retail": "retail or e-commerce business",
    "professional": "professional services firm",
}


def _industry_clause(industry: Optional[str]) -> str:
    label = _INDUSTRY_LABELS.get((industry or "").strip())
    if not label:
        return ""
    return f" The business is a {label}; fit the language and examples to it."


def build_page_prompt(
    payload: dict[str, Any], *, industry: Optional[str] = None
) -> tuple[str, str]:
    """Return (system, user) for generating a page. Pure + testable.
    `industry` defaults to the LIGHTSEI_BUSINESS_INDUSTRY env var."""
    if industry is None:
        industry = os.environ.get("LIGHTSEI_BUSINESS_INDUSTRY")
    keyword = str(payload.get("keyword") or payload.get("topic") or "").strip()
    business = str(payload.get("business_context") or payload.get("business") or "").strip()
    page_type = str(payload.get("page_type") or "landing").strip().lower()
    if page_type not in _PAGE_TYPES:
        page_type = "landing"

    parts = [
        f"Write a {page_type} page optimized for the search keyword: \"{keyword}\".",
    ]
    if business:
        parts.append(f"Business context: {business}.")
    parts.append(
        "Return a JSON object with exactly these string fields: "
        "\"title\" (an SEO title tag, 50-60 chars), "
        "\"meta_description\" (50-160 chars), "
        "\"slug\" (lowercase, hyphenated URL slug), "
        "\"h1\" (the page's main heading), and "
        "\"body_html\" (the page body as clean semantic HTML using h2/h3/p/ul "
        "tags, 300+ words, naturally including the keyword and related terms)."
    )
    system = _GEN_SYSTEM + _industry_clause(industry)
    return system, "\n\n".join(parts)


def _default_factory(api_key: str) -> Any:
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


class SEOError(Exception):
    pass


_REQUIRED_PAGE_FIELDS = ("title", "meta_description", "slug", "h1", "body_html")


def _parse_page_json(text: str) -> dict[str, Any]:
    """Tolerant parse: pull the first {...} block and load it. Raises
    SEOError if it isn't valid JSON with the required fields."""
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise SEOError("model did not return a JSON object")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise SEOError(f"model returned invalid JSON: {e}")
    missing = [f for f in _REQUIRED_PAGE_FIELDS if not str(obj.get(f) or "").strip()]
    if missing:
        raise SEOError(f"generated page missing fields: {missing}")
    return {f: str(obj.get(f)).strip() for f in _REQUIRED_PAGE_FIELDS}


def generate_page(
    payload: dict[str, Any],
    *,
    factory: ClientFactory,
    api_key: str,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """Call Claude to draft a page. Returns {page:{...}, input_tokens,
    output_tokens}. Raises SEOError on an empty / unparseable response."""
    system, user = build_page_prompt(payload)
    client = factory(api_key)
    resp = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        getattr(b, "text", "")
        for b in (getattr(resp, "content", None) or [])
        if getattr(b, "type", None) == "text"
    ).strip()
    if not text:
        raise SEOError("model returned no text")
    page = _parse_page_json(text)
    usage = getattr(resp, "usage", None)
    return {
        "page": page,
        "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
        "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
    }


# ---------- Page-idea suggestions (LLM) ---------- #

_SUGGEST_SYSTEM = (
    "You are an SEO strategist on a small business's team. You recommend new "
    "pages the business should create to win search traffic: high-intent, "
    "realistic topics a real customer would search. No fluff, no keyword "
    "stuffing, no em dashes. You return ONLY a single JSON object."
)


def build_suggest_prompt(
    payload: dict[str, Any], *, industry: Optional[str] = None
) -> tuple[str, str]:
    """Return (system, user) for suggesting pages to create. Pure + testable.
    `existing_pages` (a list of URLs/titles already on the site) steers the
    model away from duplicating what's there."""
    if industry is None:
        industry = os.environ.get("LIGHTSEI_BUSINESS_INDUSTRY")
    business = str(payload.get("business_context") or payload.get("business") or "").strip()
    existing = payload.get("existing_pages") or []
    try:
        count = int(payload.get("count") or 5)
    except (TypeError, ValueError):
        count = 5
    count = max(1, min(count, 10))

    parts = [f"Suggest {count} new pages this business should create for SEO."]
    if business:
        parts.append(f"Business context: {business}.")
    if existing:
        listed = "\n".join(f"- {p}" for p in list(existing)[:30])
        parts.append("Pages the site already has (do NOT duplicate these):\n" + listed)
    parts.append(
        "Return a JSON object with a single field \"suggestions\": an array of "
        f"{count} objects, each with \"keyword\" (the search phrase to target), "
        "\"page_type\" (one of service, location, blog, landing), and "
        "\"rationale\" (one short sentence on why it's worth creating)."
    )
    return _SUGGEST_SYSTEM + _industry_clause(industry), "\n\n".join(parts)


_SUGGEST_TYPES = {"service", "location", "blog", "landing"}


def _parse_suggestions(text: str) -> list[dict[str, Any]]:
    """Tolerant parse of the suggestions JSON. Returns a clean list of
    {keyword, page_type, rationale}; raises SEOError if unusable."""
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise SEOError("model did not return a JSON object")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as e:
        raise SEOError(f"model returned invalid JSON: {e}")
    raw = obj.get("suggestions")
    if not isinstance(raw, list) or not raw:
        raise SEOError("no suggestions in response")
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        kw = str(item.get("keyword") or "").strip()
        if not kw:
            continue
        pt = str(item.get("page_type") or "landing").strip().lower()
        if pt not in _SUGGEST_TYPES:
            pt = "landing"
        out.append({"keyword": kw, "page_type": pt,
                    "rationale": str(item.get("rationale") or "").strip()})
    if not out:
        raise SEOError("no usable suggestions in response")
    return out


def generate_suggestions(
    payload: dict[str, Any],
    *,
    factory: ClientFactory,
    api_key: str,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """Call Claude for page-idea suggestions. Returns {suggestions, tokens}."""
    system, user = build_suggest_prompt(payload)
    client = factory(api_key)
    resp = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(
        getattr(b, "text", "")
        for b in (getattr(resp, "content", None) or [])
        if getattr(b, "type", None) == "text"
    ).strip()
    if not text:
        raise SEOError("model returned no text")
    suggestions = _parse_suggestions(text)
    usage = getattr(resp, "usage", None)
    return {
        "suggestions": suggestions,
        "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
        "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
    }


# ---------- Production fetcher ---------- #


def _httpx_fetch(url: str, *, method: str = "GET") -> dict[str, Any]:
    import httpx
    try:
        resp = httpx.request(
            method, url, timeout=TIMEOUT_S, follow_redirects=True,
            headers={"User-Agent": "Lightsei-SEO-Assistant/1.0"},
        )
        return {"status_code": resp.status_code, "error": None,
                "text": resp.text if method == "GET" else ""}
    except Exception as e:
        return {"status_code": None, "error": f"{type(e).__name__}: {e}", "text": ""}


# ---------- Bot loop ---------- #


def _handle_audit(cmd_id, payload, fetch, run_id, hermes_channel):
    url = str(payload.get("url") or "").strip()
    if not url:
        lightsei.complete_command(cmd_id, error="seo.audit requires a url")
        return
    report = audit_site(url, fetch)
    report["command_id"] = cmd_id
    lightsei.emit("seo.audit_complete", report, run_id=run_id)
    # Wake the owner only when the page is unreachable or has real issues.
    if not report["reachable"] or report["issues"]:
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel, "text": hermes_text_for_audit(report),
                               "severity": report.get("severity", "warning")},
                              source_agent="seo")
        except Exception as e:
            print(f"seo: hermes dispatch failed: {e}", flush=True)
    lightsei.complete_command(cmd_id, result=report)


def _handle_crawl(cmd_id, payload, fetch, run_id, hermes_channel):
    url = str(payload.get("url") or "").strip()
    if not url:
        lightsei.complete_command(cmd_id, error="seo.crawl requires a url")
        return
    try:
        max_pages = int(payload.get("max_pages") or 5)
    except (TypeError, ValueError):
        max_pages = 5
    max_pages = max(1, min(max_pages, 15))
    report = crawl_site(url, fetch, max_pages=max_pages)
    report["command_id"] = cmd_id
    lightsei.emit("seo.crawl_complete", report, run_id=run_id)
    if report["pages_audited"] and report.get("severity") == "error":
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel, "text": hermes_text_for_crawl(report),
                               "severity": "error"}, source_agent="seo")
        except Exception as e:
            print(f"seo: hermes dispatch failed: {e}", flush=True)
    lightsei.complete_command(cmd_id, result=report)


def _handle_suggest(cmd_id, payload, run_id, hermes_channel, factory, model, max_tokens):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        lightsei.emit("seo.crash", {"command_id": cmd_id,
                                    "error": "ANTHROPIC_API_KEY not set on this workspace"}, run_id=run_id)
        lightsei.complete_command(cmd_id, error="ANTHROPIC_API_KEY not set on this workspace; add it in account settings")
        return
    try:
        result = generate_suggestions(payload, factory=factory, api_key=api_key, model=model, max_tokens=max_tokens)
    except Exception as e:
        lightsei.emit("seo.crash", {"command_id": cmd_id, "error": repr(e),
                                    "traceback": traceback.format_exc()}, run_id=run_id)
        lightsei.complete_command(cmd_id, error=repr(e))
        return
    outcome = {
        "command_id": cmd_id,
        "suggestions": result["suggestions"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": model,
        "severity": "info",
    }
    lightsei.emit("seo.suggestions", outcome, run_id=run_id)
    try:
        n = len(result["suggestions"])
        _send_with_source("hermes", "hermes.post",
                          {"channel": hermes_channel,
                           "text": f"\U0001f4a1 SEO: {n} new page idea{'s' if n != 1 else ''} for your site",
                           "severity": "info"}, source_agent="seo")
    except Exception as e:
        print(f"seo: hermes dispatch failed: {e}", flush=True)
    lightsei.complete_command(cmd_id, result=outcome)


def _handle_generate(cmd_id, payload, run_id, hermes_channel, factory, model, max_tokens):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        lightsei.emit("seo.crash", {"command_id": cmd_id,
                                    "error": "ANTHROPIC_API_KEY not set on this workspace"}, run_id=run_id)
        lightsei.complete_command(cmd_id, error="ANTHROPIC_API_KEY not set on this workspace; add it in account settings")
        return
    try:
        result = generate_page(payload, factory=factory, api_key=api_key, model=model, max_tokens=max_tokens)
    except Exception as e:
        lightsei.emit("seo.crash", {"command_id": cmd_id, "error": repr(e),
                                    "traceback": traceback.format_exc()}, run_id=run_id)
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"⚠️ SEO: couldn't draft the page ({type(e).__name__})",
                               "severity": "error"}, source_agent="seo")
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return
    page = result["page"]
    outcome = {
        "command_id": cmd_id,
        "keyword": str(payload.get("keyword") or payload.get("topic") or "").strip(),
        "page": page,
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": model,
        "severity": "info",
    }
    lightsei.emit("seo.page_drafted", outcome, run_id=run_id)
    try:
        _send_with_source("hermes", "hermes.post",
                          {"channel": hermes_channel,
                           "text": f"\U0001f4dd SEO: drafted a new page \"{page['h1']}\" for review",
                           "severity": "info"}, source_agent="seo")
    except Exception as e:
        print(f"seo: hermes dispatch failed: {e}", flush=True)
    lightsei.complete_command(cmd_id, result=outcome)


def tick(
    client: Any,
    fetch: Callable[..., dict[str, Any]] = _httpx_fetch,
    *,
    factory: ClientFactory = _default_factory,
    hermes_channel: str = "default",
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> Optional[dict[str, Any]]:
    cmd = lightsei.claim_command(agent_name="seo")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    payload = cmd.get("payload") or {}
    run_id = str(uuid.uuid4())  # explicit run_id: these events fire outside
    # an LLM-call run, and emit() drops events with no run context.

    try:
        if kind == "seo.audit":
            _handle_audit(cmd_id, payload, fetch, run_id, hermes_channel)
        elif kind == "seo.crawl":
            _handle_crawl(cmd_id, payload, fetch, run_id, hermes_channel)
        elif kind == "seo.generate_page":
            _handle_generate(cmd_id, payload, run_id, hermes_channel, factory, model, max_tokens)
        elif kind == "seo.suggest":
            _handle_suggest(cmd_id, payload, run_id, hermes_channel, factory, model, max_tokens)
        else:
            lightsei.complete_command(cmd_id, error=f"seo does not handle kind={kind!r}")
    except Exception as e:
        lightsei.emit("seo.crash", {"command_id": cmd_id, "error": repr(e),
                                    "traceback": traceback.format_exc()}, run_id=run_id)
        lightsei.complete_command(cmd_id, error=repr(e))
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "seo")
    if not api_key:
        print("seo: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"seo up: agent={agent_name} model={MODEL} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL, model=MODEL, max_tokens=MAX_TOKENS)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"seo tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
