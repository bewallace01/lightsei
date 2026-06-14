"""Business-Intelligence assistant — the AI Business Team's analyst.

Part of the roster (Phase 32.6). Turns the business's data into a plain-
English read: a weekly summary (what happened, trends, opportunities,
risks) or a direct answer to a question ("How many leads this week?",
"Why did traffic drop?"). Polls `bi.summarize` commands, asks Claude to
analyze the supplied data, emits a `bi.summary` event, and notifies the
owner via Hermes that the summary is ready.

LLM-backed, same pattern as the marketing assistant (pure prompt builder
+ generation behind an injectable factory). The data to analyze comes in
the command payload — the caller (a scheduled digest job, the dashboard,
or another assistant) gathers it; BI is the analyst that reads it.

Phase 32.6 scope: one command kind (`bi.summarize`), one downstream
dispatch (`hermes.post`), two event types (`bi.summary` + `bi.crash`),
plus a clean error when the workspace has no Anthropic key.

Env (defaults in parens):
  BI_POLL_S         seconds between claim attempts (5)
  BI_HERMES_CHANNEL channel passed to Hermes (default)
  BI_MODEL          Claude model (claude-sonnet-4-6)
  BI_MAX_TOKENS     output cap (900)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY    required (bot auth).
  ANTHROPIC_API_KEY   required for analysis.

Public surface (for tests):
  build_prompt(payload) -> (system, user)
  generate_summary(payload, *, factory, api_key, model, max_tokens) -> dict
  tick(client, *, factory=..., hermes_channel=..., model=..., max_tokens=...)
  main()
"""
import json
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

POLL_S = float(os.environ.get("BI_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("BI_HERMES_CHANNEL", "default")
MODEL = os.environ.get("BI_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("BI_MAX_TOKENS", "900"))

_SYSTEM = (
    "You are the business intelligence analyst on a small business's team. "
    "Read the data and answer for a busy, non-technical owner: short, plain "
    "English, concrete numbers, no jargon, no em dashes. When no specific "
    "question is asked, give: a one-line headline, what happened, the key "
    "trend, one opportunity, and one risk. When a question is asked, answer "
    "it directly from the data and say so if the data does not cover it."
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


def _render_data(data: Any) -> str:
    if isinstance(data, str):
        return data
    try:
        return json.dumps(data, indent=2, sort_keys=True, default=str)
    except Exception:
        return repr(data)


def build_prompt(
    payload: dict[str, Any], *, industry: Optional[str] = None
) -> tuple[str, str]:
    """Return (system, user). Pure + testable. `industry` defaults to the
    LIGHTSEI_BUSINESS_INDUSTRY env var the worker injects."""
    data = payload.get("data", {})
    question = str(payload.get("question") or "").strip()
    period = str(payload.get("period") or "").strip()

    parts: list[str] = []
    if period:
        parts.append(f"Reporting period: {period}.")
    if question:
        parts.append(f"Question to answer: {question}")
    else:
        parts.append("Produce the weekly summary.")
    parts.append("Data:\n" + _render_data(data))
    return _system_prompt(industry), "\n\n".join(parts)


# ---------- Generation (DI seam) ---------- #

ClientFactory = Callable[[str], Any]


def _default_factory(api_key: str) -> Any:
    import anthropic
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


class BIError(Exception):
    pass


def generate_summary(
    payload: dict[str, Any],
    *,
    factory: ClientFactory,
    api_key: str,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
) -> dict[str, Any]:
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
    if not text:
        raise BIError("model returned no text")
    usage = getattr(resp, "usage", None)
    return {
        "summary": text,
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
    cmd = lightsei.claim_command(agent_name="bi")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "bi.summarize":
        lightsei.complete_command(cmd_id, error=f"bi does not handle kind={kind!r}")
        return cmd

    payload = cmd.get("payload") or {}
    # Emit under an explicit run_id: these events fire outside any LLM-call
    # run, and lightsei.emit() drops events with no run context. Without
    # this the bi.summary answer never persists (the ask box + feed + digest
    # all read it). The backend creates the Run row from this id on ingest.
    run_id = str(uuid.uuid4())

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        lightsei.emit("bi.crash", {"command_id": cmd_id, "error": "ANTHROPIC_API_KEY not set on this workspace"}, run_id=run_id)
        lightsei.complete_command(cmd_id, error="ANTHROPIC_API_KEY not set on this workspace; add it in account settings")
        return cmd

    try:
        result = generate_summary(payload, factory=factory, api_key=api_key, model=model, max_tokens=max_tokens)
    except Exception as e:
        lightsei.emit("bi.crash", {"command_id": cmd_id, "error": repr(e),
                                   "traceback": traceback.format_exc()}, run_id=run_id)
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"⚠️ business intelligence: couldn't produce a summary ({type(e).__name__})",
                               "severity": "error"}, source_agent="bi")
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    is_question = bool(str(payload.get("question") or "").strip())
    outcome = {
        "command_id": cmd_id,
        "kind": "answer" if is_question else "summary",
        "summary": result["summary"],
        "input_tokens": result["input_tokens"],
        "output_tokens": result["output_tokens"],
        "model": model,
        "severity": "info",
    }
    lightsei.emit("bi.summary", outcome, run_id=run_id)

    note = "answered your question" if is_question else "your business summary is ready"
    try:
        _send_with_source("hermes", "hermes.post",
                          {"channel": hermes_channel,
                           "text": f"\U0001f4ca business intelligence: {note}",
                           "severity": "info"}, source_agent="bi")
    except Exception as e:
        print(f"bi: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "bi")
    if not api_key:
        print("bi: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"bi up: agent={agent_name} model={MODEL} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL, model=MODEL, max_tokens=MAX_TOKENS)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"bi tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
