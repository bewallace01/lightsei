"""Inbox assistant — triages the business's email.

Part of the AI Business Team (Phase 32.4), the persona that makes the
inbox manageable: for each incoming email it categorizes the message,
flags how urgent it is, drafts a reply, and writes a one-line summary,
then alerts the owner only on the urgent / needs-a-human ones. Polls
`inbox.process` commands, asks Claude to triage, emits an
`inbox.processed` event, and dispatches a `hermes.post` on urgent mail.

Payload-driven by design: the email arrives in the command payload. A
separate Gmail-poll feeder (a scheduled connector poll) is what fetches
new mail and enqueues one `inbox.process` per message; this assistant is
the triager. That keeps it self-contained + testable, same LLM-in-bot
pattern as the marketing + BI assistants.

Phase 32.4 scope: one command kind (`inbox.process`), one downstream
dispatch (`hermes.post`, only on urgent/needs-human), two event types
(`inbox.processed` + `inbox.crash`), plus a clean no-key error.

Env (defaults in parens):
  INBOX_POLL_S         seconds between claim attempts (5)
  INBOX_HERMES_CHANNEL channel passed to Hermes (default)
  INBOX_MODEL          Claude model (claude-sonnet-4-6)
  INBOX_MAX_TOKENS     output cap (700)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY    required (bot auth).
  ANTHROPIC_API_KEY   required for triage.

Public surface (for tests):
  build_prompt(email) -> (system, user)
  parse_triage(text) -> {category, urgency, summary, draft_reply, needs_human}
  generate_triage(email, *, factory, api_key, model, max_tokens) -> dict
  tick(client, *, factory=..., hermes_channel=..., model=..., max_tokens=...)
  main()
"""
import json
import os
import sys
import time
import traceback
from typing import Any, Callable, Optional

import lightsei


def _send_with_source(target_agent, kind, payload, *, source_agent):
    try:
        return lightsei.send_command(target_agent, kind, payload, source_agent=source_agent)
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("INBOX_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("INBOX_HERMES_CHANNEL", "default")
MODEL = os.environ.get("INBOX_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("INBOX_MAX_TOKENS", "700"))

_CATEGORIES = ("sales", "support", "billing", "spam", "personal", "other")
_URGENCIES = ("high", "normal", "low")

_SYSTEM = (
    "You are the inbox assistant on a small business's team. Triage one "
    "email for a busy owner. Return ONLY a JSON object, no prose, no code "
    "fences, with exactly these keys: "
    '"category" (one of sales, support, billing, spam, personal, other), '
    '"urgency" (one of high, normal, low), '
    '"summary" (one short sentence), '
    '"draft_reply" (a short, friendly reply ready to send, or "" if none is '
    'needed), '
    '"needs_human" (true if it genuinely needs the owner personally). '
    "No em dashes."
)


# Industry tailoring: the worker injects LIGHTSEI_BUSINESS_INDUSTRY from the
# owner's onboarding answers; we append a short clause so the assistant's
# voice + priorities fit the business. Empty / unknown industry = no change.
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
    return (
        f" You are working for a {label}; use language and priorities that "
        "fit that kind of business."
    )


def _system_prompt(industry: Optional[str] = None) -> str:
    if industry is None:
        industry = os.environ.get("LIGHTSEI_BUSINESS_INDUSTRY")
    return _SYSTEM + _industry_clause(industry)


# ---------- Prompt + parsing (pure) ---------- #


def build_prompt(
    email: dict[str, Any], *, industry: Optional[str] = None
) -> tuple[str, str]:
    sender = str(email.get("from") or email.get("sender") or "unknown")
    subject = str(email.get("subject") or "(no subject)")
    body = str(email.get("body") or email.get("text") or "")
    user = f"From: {sender}\nSubject: {subject}\n\n{body}".strip()
    return _system_prompt(industry), user


def parse_triage(text: str) -> dict[str, Any]:
    """Parse the model's JSON triage, tolerant of stray code fences, and
    normalize/validate the fields."""
    t = (text or "").strip()
    if t.startswith("```"):
        # drop the first fence line and any trailing fence
        inner = t.split("```")
        t = inner[1] if len(inner) > 1 else t
        if t.lstrip().lower().startswith("json"):
            t = t.lstrip()[4:]
        t = t.strip()
    obj = json.loads(t)
    if not isinstance(obj, dict):
        raise ValueError("triage was not a JSON object")
    category = str(obj.get("category") or "other").lower()
    urgency = str(obj.get("urgency") or "normal").lower()
    return {
        "category": category if category in _CATEGORIES else "other",
        "urgency": urgency if urgency in _URGENCIES else "normal",
        "summary": str(obj.get("summary") or "").strip(),
        "draft_reply": str(obj.get("draft_reply") or "").strip(),
        "needs_human": bool(obj.get("needs_human")),
    }


# ---------- Generation (DI seam) ---------- #

ClientFactory = Callable[[str], Any]


def _default_factory(api_key: str) -> Any:
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


class InboxError(Exception):
    pass


def generate_triage(
    email: dict[str, Any],
    *,
    factory: ClientFactory,
    api_key: str,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    system, user = build_prompt(email)
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
    try:
        triage = parse_triage(text)
    except (ValueError, json.JSONDecodeError) as e:
        raise InboxError(f"could not parse triage: {e}") from e
    usage = getattr(resp, "usage", None)
    triage["input_tokens"] = getattr(usage, "input_tokens", 0) if usage else 0
    triage["output_tokens"] = getattr(usage, "output_tokens", 0) if usage else 0
    return triage


# ---------- Bot loop ---------- #


def tick(
    client: Any,
    *,
    factory: ClientFactory = _default_factory,
    hermes_channel: str = "default",
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> Optional[dict[str, Any]]:
    cmd = lightsei.claim_command(agent_name="inbox")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "inbox.process":
        lightsei.complete_command(cmd_id, error=f"inbox does not handle kind={kind!r}")
        return cmd

    payload = cmd.get("payload") or {}
    email = payload.get("email") if isinstance(payload.get("email"), dict) else payload

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        lightsei.emit("inbox.crash", {"command_id": cmd_id, "error": "ANTHROPIC_API_KEY not set on this workspace"})
        lightsei.complete_command(cmd_id, error="ANTHROPIC_API_KEY not set on this workspace; add it in account settings")
        return cmd

    try:
        triage = generate_triage(email, factory=factory, api_key=api_key, model=model, max_tokens=max_tokens)
    except Exception as e:
        lightsei.emit("inbox.crash", {"command_id": cmd_id, "error": repr(e),
                                      "traceback": traceback.format_exc()})
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"⚠️ inbox: couldn't triage an email ({type(e).__name__})",
                               "severity": "error"}, source_agent="inbox")
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    flagged = triage["urgency"] == "high" or triage["needs_human"]
    outcome = {
        "command_id": cmd_id,
        "from": str(email.get("from") or email.get("sender") or "unknown"),
        "subject": str(email.get("subject") or ""),
        "category": triage["category"],
        "urgency": triage["urgency"],
        "summary": triage["summary"],
        "draft_reply": triage["draft_reply"],
        "needs_human": triage["needs_human"],
        "input_tokens": triage["input_tokens"],
        "output_tokens": triage["output_tokens"],
        "model": model,
        "severity": "error" if flagged else "info",
    }
    lightsei.emit("inbox.processed", outcome)

    # Only interrupt the owner for urgent / needs-a-human mail. Everything
    # else is triaged + drafted in the event stream without a ping.
    if flagged:
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"\U0001f4e7 inbox: urgent {triage['category']} email from "
                                       f"{outcome['from']} — {triage['summary']}",
                               "severity": "error"}, source_agent="inbox")
        except Exception as e:
            print(f"inbox: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "inbox")
    if not api_key:
        print("inbox: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"inbox up: agent={agent_name} model={MODEL} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL, model=MODEL, max_tokens=MAX_TOKENS)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"inbox tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
