"""Trigger the Design assistant (Capella) and read its result.

Capella formats/styles content on the worker, so this is async (same shape
as ask.py): enqueue a `design.format` command, then poll for the
`design.formatted` event by command id. Generic on purpose — any surface
(SEO drafts, marketing, a future formatting box) can format content through
it.
"""
from __future__ import annotations

import json
import re
import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

DESIGN_AGENT = "design"
FORMAT_KIND = "design.format"
SOURCE = "dashboard"

_COMMAND_TTL = timedelta(hours=24)
_MAX_CONTENT_LEN = 60_000
CONTENT_TYPES = ("page", "email", "social", "generic", "component")


# ---------- "Match my site" style extraction ---------- #

_FONT_RE = re.compile(r"font-family\s*:\s*([^;}\"']+)", re.IGNORECASE)
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b")
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style>", re.IGNORECASE | re.DOTALL)
_TAILWIND_RE = re.compile(r'class="[^"]*\b(?:flex|grid|px-\d|py-\d|text-(?:sm|lg|xl)|bg-\w+-\d{2,3}|rounded-\w+)\b', re.IGNORECASE)
_BOOTSTRAP_RE = re.compile(r'class="[^"]*\b(?:container|row|col-\w+|btn-(?:primary|secondary)|navbar)\b', re.IGNORECASE)

# A CSS rule: "selector(s) { declarations }". Used to read colors by ROLE
# (what is the body background? the heading color?) instead of frequency-
# counting every hex on the page, which skews toward palette/utility
# definitions that have nothing to do with the visible design.
_RULE_RE = re.compile(r"([^{}]+)\{([^{}]+)\}", re.DOTALL)
_DECL_RE = re.compile(r"([a-zA-Z-]+)\s*:\s*([^;]+)")
# Tailwind background/text utilities actually placed on a tag (the real signal
# for utility-class sites): bg-white, bg-slate-900, text-gray-900, bg-stone-50.
_TW_BG_RE = re.compile(r"\bbg-(white|black|(?:slate|gray|zinc|neutral|stone|amber|orange|red|rose|emerald|green|blue|indigo|violet|purple|sky|teal|cyan)-\d{2,3})\b")
_TW_TEXT_RE = re.compile(r"\btext-(white|black|(?:slate|gray|zinc|neutral|stone)-\d{2,3})\b")

# Tailwind shade -> rough lightness. High shade number = dark for most hues;
# the named light/dark anchors are explicit.
def _tw_is_dark(util: str) -> Optional[bool]:
    """True if a Tailwind color utility (e.g. 'slate-900', 'white') reads as
    dark, False if light, None if ambiguous."""
    if util == "white":
        return False
    if util == "black":
        return True
    m = re.search(r"-(\d{2,3})$", util)
    if not m:
        return None
    shade = int(m.group(1))
    if shade <= 200:
        return False
    if shade >= 600:
        return True
    return None


def _norm_hex(value: str) -> Optional[str]:
    """Pull the first hex color out of a CSS value and normalize to #rrggbb.
    Handles #rgb shorthand. Returns None if there's no hex."""
    v = value.strip().lower()
    m = re.search(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", v)
    if not m:
        return None
    h = m.group(1)
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h


def _luminance(hex6: str) -> float:
    """Relative luminance 0 (black) .. 1 (white) of a #rrggbb color."""
    h = hex6.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _iter_rules(css: str):
    for sel, body in _RULE_RE.findall(css):
        decls = {}
        for prop, val in _DECL_RE.findall(body):
            decls[prop.strip().lower()] = val.strip()
        yield sel.strip().lower(), decls


def _role_color(rules: list[tuple[str, dict]], selectors: tuple[str, ...], props: tuple[str, ...]) -> Optional[str]:
    """First hex color for one of `props` on a rule whose selector matches one
    of `selectors` (substring match on a normalized selector list)."""
    for sel, decls in rules:
        sel_parts = [s.strip() for s in sel.split(",")]
        if not any(s == want or s.endswith(" " + want) or s.startswith(want)
                   for s in sel_parts for want in selectors):
            continue
        for p in props:
            if p in decls:
                hx = _norm_hex(decls[p])
                if hx:
                    return hx
    return None


def _first_tag_classes(html: str, tag: str) -> str:
    m = re.search(rf"<{tag}\b[^>]*\bclass=\"([^\"]*)\"", html, re.IGNORECASE)
    return m.group(1).lower() if m else ""


def _collect_css(html: str) -> str:
    return " ".join(" ".join(_STYLE_BLOCK_RE.findall(html)).split())


# Base elements whose rules describe the actual visual design (vs utility-class
# definitions like `.bg-slate-900{}` that are framework noise).
_BASE_SELECTORS = ("body", "html", ":root", "h1", "h2", "h3", "h4", "p", "a",
                   "header", "nav", "main", "footer", "section", "blockquote",
                   "button", "li")


def _base_css_sample(rules: list[tuple[str, dict]], *, max_chars: int) -> str:
    """Reconstruct a CSS sample from rules that target base HTML elements, so a
    compiled Tailwind/utility bundle's palette rules don't drown out (or
    mislead with) the real design. Empty string if nothing base-level."""
    out: list[str] = []
    for sel, decls in rules:
        sel_parts = [s.strip() for s in sel.split(",")]
        if not any(s == want or s.startswith(want + " ") or s.startswith(want + ":")
                   or s == want for s in sel_parts for want in _BASE_SELECTORS):
            continue
        body = "; ".join(f"{p}: {v}" for p, v in decls.items())
        if body:
            out.append(f"{sel} {{ {body} }}")
        if sum(len(x) for x in out) > max_chars:
            break
    return " ".join(out)[:max_chars]


def _detect_theme(html: str, rules: list[tuple[str, dict]], bg: Optional[str]) -> Optional[str]:
    """Return 'LIGHT' or 'DARK' (or None if undeterminable). Prefer the actual
    body/html background color; fall back to Tailwind bg utilities on body."""
    if bg:
        return "DARK" if _luminance(bg) < 0.5 else "LIGHT"
    # Tailwind utility sites set the theme via classes on <body>/<main>.
    for tag in ("body", "main", "html"):
        classes = _first_tag_classes(html, tag)
        m = _TW_BG_RE.search(classes)
        if m:
            dark = _tw_is_dark(m.group(1))
            if dark is not None:
                return "DARK" if dark else "LIGHT"
    # Last resort: tally bg-*/text-* utilities used anywhere on the page. A
    # mostly-light page has many light backgrounds + dark text and vice versa.
    light = dark = 0
    for m in _TW_BG_RE.finditer(html):
        d = _tw_is_dark(m.group(1))
        if d is True:
            dark += 1
        elif d is False:
            light += 1
    for m in _TW_TEXT_RE.finditer(html):
        d = _tw_is_dark(m.group(1))
        # Dark TEXT implies a LIGHT page, and vice versa.
        if d is True:
            light += 1
        elif d is False:
            dark += 1
    if light + dark >= 3 and light != dark:
        return "LIGHT" if light > dark else "DARK"
    return None


def extract_style_profile(html: str, *, max_chars: int = 1200) -> Optional[str]:
    """Read a page's HTML and return a compact, forceful style guide Capella
    can use to REPRODUCE the site's look. Reads colors by role (background,
    text, headings, links) and the light/dark theme, rather than frequency-
    counting hexes (which skews to palette/utility definitions, not the visible
    design). Pure + testable. Returns None if nothing useful was found."""
    html = html or ""
    css = _collect_css(html)
    rules = list(_iter_rules(css))

    bg = _role_color(rules, ("body", "html", ":root"), ("background-color", "background"))
    text = _role_color(rules, ("body", "html"), ("color",))
    heading = _role_color(rules, ("h1", "h2", "h3"), ("color",))
    link = _role_color(rules, ("a",), ("color",))
    theme = _detect_theme(html, rules, bg)

    # Fonts: try to separate heading vs body. Falls back to the first few seen.
    fonts: list[str] = []
    for m in _FONT_RE.findall(css or html):
        f = " ".join(m.split()).strip().strip("'\"")
        if f and f.lower() not in [x.lower() for x in fonts]:
            fonts.append(f)

    lines: list[str] = []
    if theme:
        if theme == "LIGHT":
            lines.append(
                "Theme: LIGHT. Use a light/near-white page background and dark "
                "text. Do NOT use a dark background or a dark theme.")
        else:
            lines.append(
                "Theme: DARK. Use a dark page background and light text. Do NOT "
                "use a light background.")
    if bg:
        lines.append(f"Page background color: {bg}.")
    if text:
        lines.append(f"Body text color: {text}.")
    if heading:
        lines.append(f"Heading color: {heading}.")
    if link:
        lines.append(f"Link / accent color: {link}.")
    if fonts:
        lines.append("Fonts (first is most likely headings, then body): "
                     + "; ".join(fonts[:3]) + ".")
    if _TAILWIND_RE.search(html):
        lines.append("The site uses Tailwind CSS utility classes.")
    elif _BOOTSTRAP_RE.search(html):
        lines.append("The site uses Bootstrap.")
    sample = _base_css_sample(rules, max_chars=max_chars)
    if sample:
        lines.append("A sample of the site's base-element CSS to mirror:\n" + sample)

    if not lines:
        return None
    return (
        "Reproduce this existing website's visual design EXACTLY. Match its "
        "theme, background, text colors, fonts, and spacing. Do not invent a "
        "different look or flip the theme. Details:\n" + "\n".join(lines))


def primary_color(html: str) -> Optional[str]:
    """A good accent guess: the site's link color if we can find one, else a
    saturated brand hue that isn't the background or text. Avoids returning the
    page background (which the old frequency-count often did, producing pale
    'accent' colors like a cream background)."""
    html = html or ""
    rules = list(_iter_rules(_collect_css(html)))
    link = _role_color(rules, ("a",), ("color",))
    if link:
        return link
    bg = _role_color(rules, ("body", "html", ":root"), ("background-color", "background"))
    text = _role_color(rules, ("body", "html"), ("color",))
    skip = {"#ffffff", "#000000"}
    if bg:
        skip.add(bg)
    if text:
        skip.add(text)
    counts = Counter(c.lower() for c in _HEX_RE.findall(html) if c.lower() not in skip)
    # Prefer a saturated color (max-min channel spread) over a near-grey, and
    # avoid extremes (near-black / near-white) so we don't hand a light page a
    # dark "accent" or vice versa. Better to return None (model picks a
    # tasteful default) than a misleading color.
    def _sat(hx: str) -> int:
        h = hx.lstrip("#")
        r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
        return max(r, g, b) - min(r, g, b)
    for hx, _ in counts.most_common(12):
        if _sat(hx) >= 40 and 0.16 <= _luminance(hx) <= 0.85:
            return hx
    return None


def enqueue_format(
    session: Session,
    workspace_id: str,
    *,
    content: str,
    content_type: str,
    accent_color: Optional[str] = None,
    instructions: Optional[str] = None,
    template: Optional[str] = None,
    now: datetime,
) -> str:
    """Enqueue a design.format command for Capella. Returns the command id.
    Does not commit. Caller validates content is non-empty.

    `template`: an existing page's source from the owner's repo. When given
    with content_type 'component', Capella writes a new page that matches it.
    """
    ct = (content_type or "generic").strip().lower()
    if ct not in CONTENT_TYPES:
        ct = "generic"
    payload: dict[str, Any] = {
        "source": SOURCE,
        "content": (content or "")[:_MAX_CONTENT_LEN],
        "content_type": ct,
    }
    if accent_color and accent_color.strip():
        payload["accent_color"] = accent_color.strip()[:32]
    if instructions and instructions.strip():
        payload["instructions"] = instructions.strip()[:500]
    if template and template.strip():
        payload["template"] = template[:_MAX_CONTENT_LEN]

    cmd_id = str(uuid.uuid4())
    session.execute(
        text(
            """
            INSERT INTO commands (
                id, workspace_id, agent_name, kind, payload, status,
                approval_state, approved_at, created_at, expires_at,
                dispatch_chain_id, dispatch_depth
            ) VALUES (
                :id, :ws, :agent, :kind, CAST(:payload AS JSONB), 'pending',
                'auto_approved', :now, :now, :expires, :chain, 0
            )
            """
        ),
        {
            "id": cmd_id, "ws": workspace_id, "agent": DESIGN_AGENT,
            "kind": FORMAT_KIND, "payload": json.dumps(payload),
            "now": now, "expires": now + _COMMAND_TTL, "chain": cmd_id,
        },
    )
    return cmd_id


def get_result(session: Session, workspace_id: str, command_id: str) -> dict[str, Any]:
    """Poll for a format result. Returns one of:
      {"status": "formatted", "output": str, "content_type": str}
      {"status": "failed", "error": str}
      {"status": "pending"}
    Matches design.formatted (success) / design.crash (failure) by the
    command_id Capella stamps into each event payload. Workspace-scoped."""
    done = session.execute(
        text(
            """
            SELECT payload FROM events
             WHERE workspace_id = :ws AND kind = 'design.formatted'
               AND payload ->> 'command_id' = :cid
             ORDER BY timestamp DESC LIMIT 1
            """
        ),
        {"ws": workspace_id, "cid": command_id},
    ).first()
    if done is not None:
        p = done[0] or {}
        return {"status": "formatted", "output": p.get("output") or "",
                "content_type": p.get("content_type")}

    crash = session.execute(
        text(
            """
            SELECT payload FROM events
             WHERE workspace_id = :ws AND kind = 'design.crash'
               AND payload ->> 'command_id' = :cid
             ORDER BY timestamp DESC LIMIT 1
            """
        ),
        {"ws": workspace_id, "cid": command_id},
    ).first()
    if crash is not None:
        return {"status": "failed", "error": (crash[0] or {}).get("error") or "design failed"}

    return {"status": "pending"}


def design_deployed(session: Session, workspace_id: str) -> bool:
    """Whether the Design assistant exists for this workspace."""
    row = session.execute(
        text("SELECT 1 FROM agents WHERE workspace_id = :ws AND name = :n"),
        {"ws": workspace_id, "n": DESIGN_AGENT},
    ).first()
    return row is not None
