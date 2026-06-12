"""Marketing assistant — the AI Business Team's marketing coordinator.

Part of the roster (Phase 32.4). Turns a plain-English request into real
marketing content: ad copy, social posts, campaign ideas, a marketing
email. Polls `marketing.create` commands, asks Claude to write the
content, emits a `marketing.created` event with the draft, and dispatches
a `hermes.post` "draft ready" note to the owner.

This is the first LLM-backed persona: it calls Claude with the workspace's
own `ANTHROPIC_API_KEY` (injected by the worker from workspace secrets),
the same key the team router uses. The Anthropic client is created
through an injectable factory so tests can stub it.

Phase 32.4 scope: one command kind (`marketing.create`), one downstream
dispatch (`hermes.post`), two event types (`marketing.created` +
`marketing.crash`). Plus a clean error when the workspace has no key.

Env (defaults in parens):
  MARKETING_POLL_S         seconds between claim attempts (5)
  MARKETING_HERMES_CHANNEL channel passed to Hermes (default)
  MARKETING_MODEL          Claude model (claude-sonnet-4-6)
  MARKETING_MAX_TOKENS     output cap (800)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY    required (bot auth).
  ANTHROPIC_API_KEY   required for generation; a clean error otherwise.

Public surface (for tests):
  build_prompt(task, payload) -> (system, user)
  generate_content(task, payload, *, factory, api_key, model, max_tokens) -> dict
  tick(client, *, factory=..., hermes_channel=..., model=..., max_tokens=...)
  main()
"""
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

POLL_S = float(os.environ.get("MARKETING_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("MARKETING_HERMES_CHANNEL", "default")
MODEL = os.environ.get("MARKETING_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("MARKETING_MAX_TOKENS", "800"))

_TASKS = ("ad_copy", "social_post", "campaign_idea", "email_copy")

_SYSTEM = (
    "You are the marketing coordinator on a small business's team. Write in "
    "the business's voice: clear, friendly, concrete, no corporate fluff, no "
    "em dashes. Match the requested platform and tone. Output only the "
    "finished content, ready to use."
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
        f" You are working for a {label}; use language, examples, and "
        "priorities that fit that kind of business."
    )


def _system_prompt(industry: Optional[str] = None) -> str:
    if industry is None:
        industry = os.environ.get("LIGHTSEI_BUSINESS_INDUSTRY")
    return _SYSTEM + _industry_clause(industry)


# ---------- Prompt building (pure) ---------- #


def build_prompt(
    task: str, payload: dict[str, Any], *, industry: Optional[str] = None
) -> tuple[str, str]:
    """Return (system, user) for the requested task. Pure + testable.
    `industry` defaults to the LIGHTSEI_BUSINESS_INDUSTRY env var."""
    topic = str(payload.get("topic") or payload.get("prompt") or "").strip()
    business = str(payload.get("business_context") or payload.get("business") or "").strip()
    platform = str(payload.get("platform") or "").strip()
    tone = str(payload.get("tone") or "friendly and professional").strip()

    ctx = []
    if business:
        ctx.append(f"Business: {business}.")
    if platform:
        ctx.append(f"Platform: {platform}.")
    ctx.append(f"Tone: {tone}.")
    context = " ".join(ctx)

    if task == "social_post":
        ask = f"Write a short social media post about: {topic}. Include 2-3 relevant hashtags."
    elif task == "campaign_idea":
        ask = f"Suggest 3 concrete marketing campaign ideas for: {topic}. One short paragraph each."
    elif task == "email_copy":
        ask = f"Write a marketing email about: {topic}. Include a subject line and a clear call to action."
    else:  # ad_copy (default)
        ask = f"Write punchy ad copy for: {topic}. Give a headline and 1-2 lines of body, plus a call to action."

    user = f"{ask}\n\n{context}".strip()
    return _system_prompt(industry), user


# ---------- Generation (DI seam) ---------- #

ClientFactory = Callable[[str], Any]


def _default_factory(api_key: str) -> Any:
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


class MarketingError(Exception):
    pass


def generate_content(
    task: str,
    payload: dict[str, Any],
    *,
    factory: ClientFactory,
    api_key: str,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
    """Call Claude to produce the content. Returns {content, input_tokens,
    output_tokens}. Raises MarketingError on an empty/odd response."""
    system, user = build_prompt(task, payload)
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
        raise MarketingError("model returned no text")
    usage = getattr(resp, "usage", None)
    return {
        "content": text,
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
    cmd = lightsei.claim_command(agent_name="marketing")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "marketing.create":
        lightsei.complete_command(cmd_id, error=f"marketing does not handle kind={kind!r}")
        return cmd

    payload = cmd.get("payload") or {}
    task = str(payload.get("task") or "ad_copy")
    if task not in _TASKS:
        lightsei.complete_command(cmd_id, error=f"unknown marketing task {task!r}; expected one of {_TASKS}")
        return cmd

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        # Clean, actionable failure rather than a crash: the owner needs to
        # connect a key. Surface it on the command + an event.
        lightsei.emit("marketing.crash", {"command_id": cmd_id, "error": "ANTHROPIC_API_KEY not set on this workspace"})
        lightsei.complete_command(cmd_id, error="ANTHROPIC_API_KEY not set on this workspace; add it in account settings")
        return cmd

    try:
        result = generate_content(task, payload, factory=factory, api_key=api_key, model=model, max_tokens=max_tokens)
    except Exception as e:
        lightsei.emit("marketing.crash", {"command_id": cmd_id, "error": repr(e),
                                          "traceback": traceback.format_exc()})
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"⚠️ marketing: couldn't generate {task} ({type(e).__name__})",
                               "severity": "error"}, source_agent="marketing")
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    outcome = {
        "command_id": cmd_id,
        "task": task,
        "content": result["content"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": model,
        "severity": "info",
    }
    lightsei.emit("marketing.created", outcome)

    try:
        _send_with_source("hermes", "hermes.post",
                          {"channel": hermes_channel,
                           "text": f"✨ marketing: your {task.replace('_', ' ')} draft is ready",
                           "severity": "info"}, source_agent="marketing")
    except Exception as e:
        print(f"marketing: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "marketing")
    if not api_key:
        print("marketing: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"marketing up: agent={agent_name} model={MODEL} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL, model=MODEL, max_tokens=MAX_TOKENS)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"marketing tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
