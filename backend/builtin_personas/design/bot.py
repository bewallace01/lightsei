"""Design assistant — the AI Business Team's design + formatting specialist.

Part of the roster. Takes any content the team produces — an SEO page, a
marketing email, a social post, or generic copy — and makes it look good:
clean typography, spacing, a tasteful layout, responsive HTML where the
medium calls for it. The *words* stay the owner's; this assistant improves
the *presentation*.

LLM-backed (same pattern as marketing/bi/seo): a pure prompt builder + a
generation step behind an injectable client factory.

Command kind: `design.format`.
Events: `design.formatted`, `design.crash`.
Downstream: one `hermes.post` ("polished and ready").

Content types (payload.content_type):
  page    -> complete, responsive HTML with embedded <style> (a real,
             good-looking web page; content + headings preserved).
  email   -> a clean, email-safe HTML layout (inline-ish styles, single
             column) suitable for sending.
  social  -> nicely structured plain text (line breaks, tasteful emoji,
             hashtags) for a social post.
  generic -> improved formatting + visual structure of whatever is given.

Env (defaults in parens):
  DESIGN_POLL_S         seconds between claim attempts (5)
  DESIGN_HERMES_CHANNEL channel passed to Hermes (default)
  DESIGN_MODEL          Claude model (claude-sonnet-4-6)
  DESIGN_MAX_TOKENS     output cap (2000)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY    required (bot auth).
  ANTHROPIC_API_KEY   required for formatting.

Public surface (for tests):
  build_prompt(payload) -> (system, user)
  generate_design(payload, *, factory, api_key, model, max_tokens) -> dict
  tick(client, *, factory=..., hermes_channel=..., model=..., max_tokens=...)
  main()
"""
import os
import sys
import time
import traceback
import uuid
from typing import Any, Callable, Optional

import lightsei


def _send_with_source(target_agent, kind, payload, *, source_agent):
    try:
        return lightsei.send_command(target_agent, kind, payload, source_agent=source_agent)
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("DESIGN_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("DESIGN_HERMES_CHANNEL", "default")
MODEL = os.environ.get("DESIGN_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("DESIGN_MAX_TOKENS", "2000"))

CONTENT_TYPES = ("page", "email", "social", "generic", "component")
# A full framework page component can be large; give it more room.
COMPONENT_MAX_TOKENS = int(os.environ.get("DESIGN_COMPONENT_MAX_TOKENS", "6000"))

_SYSTEM = (
    "You are the design specialist on a small business's team. You take "
    "content and make it look polished and professional without changing the "
    "wording or meaning: clean typography, clear hierarchy, comfortable "
    "spacing, a tasteful accent color, mobile-friendly. No corporate "
    "clutter, no lorem ipsum, no em dashes. Output ONLY the finished "
    "content, ready to use, with no commentary or code fences."
)

# For component mode: a careful front-end engineer who writes a new page that
# fits an existing codebase exactly (imports, layout, framework, styling).
_SYSTEM_COMPONENT = (
    "You are a meticulous front-end engineer adding a page to an existing "
    "codebase. You mirror the project's framework, imports, layout wrappers, "
    "shared components, routing/meta patterns, and styling conventions exactly "
    "so the new page is indistinguishable in structure from the others. You "
    "change only the page's content. No em dashes. Output ONLY the new file's "
    "raw source code, no commentary and no markdown code fences."
)


def _instructions(payload: dict[str, Any]) -> str:
    return str(payload.get("instructions") or payload.get("style_hint") or "").strip()


def _accent(payload: dict[str, Any]) -> str:
    return str(payload.get("accent_color") or "").strip()


def build_prompt(payload: dict[str, Any]) -> tuple[str, str]:
    """Return (system, user) for the requested formatting. Pure + testable."""
    content = str(payload.get("content") or "").strip()
    ctype = str(payload.get("content_type") or "generic").strip().lower()
    if ctype not in CONTENT_TYPES:
        ctype = "generic"
    extra = _instructions(payload)
    accent = _accent(payload)

    if ctype == "component":
        # Match an existing page from the owner's codebase. The template is an
        # actual page file from their repo; produce a NEW page in the exact
        # same framework, imports, layout, and conventions, with new content.
        template = str(payload.get("template") or "").strip()
        ask = (
            "Below is an existing page from a website's codebase (TEMPLATE) and "
            "the content for a new page (NEW CONTENT). Write a NEW page file in "
            "the EXACT same framework, language, and conventions as the "
            "template: the same import statements, the same layout/wrapper and "
            "shared components, the same routing/meta/SEO patterns, the same "
            "styling approach (CSS classes, Tailwind, etc.). Only the page's "
            "actual content (headings, paragraphs, sections) should change to "
            "the NEW CONTENT. Keep it production-ready and self-consistent. "
            "Return ONLY the new file's source code, no commentary, no code "
            "fences."
        )
        parts = [ask, "TEMPLATE (an existing page from this site):\n" + template,
                 "NEW CONTENT for the new page:\n" + content]
        if extra:
            parts.append("Extra direction: " + extra)
        return _SYSTEM_COMPONENT, "\n\n".join(parts)

    if ctype == "page":
        ask = (
            "Restyle this web page into a complete, modern, responsive HTML "
            "document. Keep all of the words, headings, and links exactly. "
            "Return a full <!doctype html> document with an embedded <style> "
            "block: readable typography, generous spacing, a centered content "
            "column (max-width ~720px), a tasteful accent color, and good "
            "mobile rendering."
        )
    elif ctype == "email":
        ask = (
            "Format this into a clean, email-safe HTML message: a single "
            "centered column, system fonts, inline-friendly styles, a clear "
            "heading and a button-style call to action if one is implied. "
            "Keep the wording."
        )
    elif ctype == "social":
        ask = (
            "Reformat this social post for readability: short lines, line "
            "breaks between ideas, 1-3 tasteful emoji where they help, and "
            "relevant hashtags at the end. Keep the message."
        )
    else:  # generic
        ask = (
            "Improve the formatting and visual structure of this content "
            "(headings, lists, spacing, emphasis) without changing the words."
        )

    parts = [ask]
    if accent:
        parts.append(f"Use {accent} as the accent color.")
    if extra:
        parts.append(f"Extra direction: {extra}")
    parts.append("Content:\n" + content)
    return _SYSTEM, "\n\n".join(parts)


# ---------- Generation (DI seam) ---------- #

ClientFactory = Callable[[str], Any]


def _default_factory(api_key: str) -> Any:
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


class DesignError(Exception):
    pass


def generate_design(
    payload: dict[str, Any],
    *,
    factory: ClientFactory,
    api_key: str,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """Call Claude to format the content. Returns {output, input_tokens,
    output_tokens}. Raises DesignError on empty input/output."""
    if not str(payload.get("content") or "").strip():
        raise DesignError("no content to format")
    system, user = build_prompt(payload)
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
    # Strip a stray code fence if the model wrapped the output despite the
    # instruction not to.
    if text.startswith("```"):
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        raise DesignError("model returned no text")
    usage = getattr(resp, "usage", None)
    return {
        "output": text,
        "input_tokens": getattr(usage, "input_tokens", 0) if usage else 0,
        "output_tokens": getattr(usage, "output_tokens", 0) if usage else 0,
    }


# ---------- Bot loop ---------- #


def tick(
    client: Any,
    *,
    factory: ClientFactory = _default_factory,
    hermes_channel: str = "default",
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> Optional[dict[str, Any]]:
    cmd = lightsei.claim_command(agent_name="design")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "design.format":
        lightsei.complete_command(cmd_id, error=f"design does not handle kind={kind!r}")
        return cmd

    payload = cmd.get("payload") or {}
    run_id = str(uuid.uuid4())  # explicit run_id: these events fire outside
    # an LLM-call run, and emit() drops events with no run context.
    ctype = str(payload.get("content_type") or "generic").strip().lower()
    if ctype not in CONTENT_TYPES:
        ctype = "generic"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        lightsei.emit("design.crash", {"command_id": cmd_id, "error": "ANTHROPIC_API_KEY not set on this workspace"}, run_id=run_id)
        lightsei.complete_command(cmd_id, error="ANTHROPIC_API_KEY not set on this workspace; add it in account settings")
        return cmd

    # A full page component needs more output room than a styled snippet.
    eff_max_tokens = COMPONENT_MAX_TOKENS if ctype == "component" else max_tokens
    try:
        result = generate_design(payload, factory=factory, api_key=api_key, model=model, max_tokens=eff_max_tokens)
    except Exception as e:
        lightsei.emit("design.crash", {"command_id": cmd_id, "error": repr(e),
                                       "traceback": traceback.format_exc()}, run_id=run_id)
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"⚠️ design: couldn't format the {ctype} ({type(e).__name__})",
                               "severity": "error"}, source_agent="design")
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    outcome = {
        "command_id": cmd_id,
        "content_type": ctype,
        "output": result["output"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": model,
        "severity": "info",
    }
    lightsei.emit("design.formatted", outcome, run_id=run_id)

    try:
        _send_with_source("hermes", "hermes.post",
                          {"channel": hermes_channel,
                           "text": f"\U0001f3a8 design: polished your {ctype} — ready to use",
                           "severity": "info"}, source_agent="design")
    except Exception as e:
        print(f"design: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "design")
    if not api_key:
        print("design: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"design up: agent={agent_name} model={MODEL} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL, model=MODEL, max_tokens=MAX_TOKENS)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"design tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
