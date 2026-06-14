"""Lead-Management assistant — never lets a lead fall through the cracks.

Part of the AI Business Team (Phase 32.2). When a new lead comes in (from
the website assistant's widget, a form, etc.), this assistant scores its
quality, decides whether it's due for follow-up, and suggests the next
action — then alerts the owner for the leads worth acting on now and
stays quiet on the rest. Polls `lead.process` commands, emits a
`lead.scored` event, dispatches a `hermes.post` for hot/actionable leads.

Pure scoring + follow-up logic (no LLM), same bot contract as the rest of
the constellation.

Phase 32.2 scope: one command kind (`lead.process`), one downstream
dispatch (`hermes.post`), two event types (`lead.scored` + `lead.crash`).

Env (defaults in parens):
  LEAD_POLL_S            seconds between claim attempts (5)
  LEAD_HERMES_CHANNEL    channel passed to Hermes (default)
  LEAD_FOLLOWUP_HOURS    hours since last contact before follow-up is due (48)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required.

Public surface (for tests):
  score_lead(lead) -> {score, quality, reasons}
  needs_followup(lead, *, now, window_hours) -> bool
  suggest_next_action(quality, needs_followup) -> str
  tick(client, *, hermes_channel=..., followup_hours=...)
  main()
"""
import os
import uuid
import re
import sys
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Optional

import lightsei


def _send_with_source(target_agent, kind, payload, *, source_agent):
    try:
        return lightsei.send_command(target_agent, kind, payload, source_agent=source_agent)
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("LEAD_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("LEAD_HERMES_CHANNEL", "default")
FOLLOWUP_HOURS = float(os.environ.get("LEAD_FOLLOWUP_HOURS", "48"))

_INTENT_WORDS = (
    "buy", "purchase", "pricing", "price", "quote", "demo", "trial",
    "interested", "hire", "book", "appointment", "urgent", "asap", "today",
    "sign up", "get started", "ready",
)
_BUDGET_RE = re.compile(r"\$\s?\d|\b\d+\s?(k|grand|thousand|/mo|per month)\b", re.IGNORECASE)
_CLOSED_STATUSES = {"won", "lost", "closed", "unqualified"}


# ---------- Pure scoring ---------- #


def score_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """Quality score 0-100 with the reasons that drove it. Heuristic, no
    LLM: contactability (email/phone) + qualification signals (company,
    message intent, budget)."""
    score = 0
    reasons: list[str] = []

    def add(points: int, reason: str) -> None:
        nonlocal score
        score += points
        reasons.append(reason)

    if lead.get("email"):
        add(25, "has email")
    if lead.get("phone"):
        add(20, "has phone")
    if lead.get("company"):
        add(10, "has company")
    if lead.get("name"):
        add(5, "has name")

    message = str(lead.get("message") or "")
    if len(message.strip()) >= 20:
        add(10, "wrote a real message")
    low = message.lower()
    if any(w in low for w in _INTENT_WORDS):
        add(20, "message shows buying intent")
    if _BUDGET_RE.search(message) or lead.get("budget"):
        add(10, "mentioned budget")

    score = min(score, 100)
    quality = "hot" if score >= 70 else "warm" if score >= 40 else "cold"
    return {"score": score, "quality": quality, "reasons": reasons}


def needs_followup(lead: dict[str, Any], *, now: datetime, window_hours: float = 48.0) -> bool:
    """True if this lead is due for a follow-up: not closed, and either
    never contacted or last contacted more than `window_hours` ago."""
    if str(lead.get("status") or "").strip().lower() in _CLOSED_STATUSES:
        return False
    last = lead.get("last_contact_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True  # unparseable -> treat as never contacted
    return (now - dt).total_seconds() > window_hours * 3600


def suggest_next_action(quality: str, due: bool) -> str:
    if not due:
        return "Waiting on the lead's reply — no action needed yet"
    if quality == "hot":
        return "Call now — hot lead worth a personal touch"
    if quality == "warm":
        return "Send a personalized follow-up email today"
    return "Add to the nurture sequence"


def _lead_label(lead: dict[str, Any]) -> str:
    return str(lead.get("name") or lead.get("email") or lead.get("company") or "a lead")


def hermes_text_for(lead: dict[str, Any], scored: dict[str, Any], action: str) -> str:
    icon = "\U0001f525" if scored["quality"] == "hot" else "\U0001f4cc"
    return (
        f"{icon} lead: {scored['quality']} lead from {_lead_label(lead)} "
        f"(score {scored['score']}) — {action}"
    )


# ---------- Bot loop ---------- #


def tick(client: Any, *, hermes_channel: str = "default", followup_hours: float = 48.0) -> Optional[dict[str, Any]]:
    cmd = lightsei.claim_command(agent_name="lead")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "lead.process":
        lightsei.complete_command(cmd_id, error=f"lead does not handle kind={kind!r}")
        return cmd

    payload = cmd.get("payload") or {}
    run_id = str(uuid.uuid4())  # explicit run_id: these events fire outside
    # an LLM-call run, and emit() drops events with no run context.
    lead = payload.get("lead") if isinstance(payload.get("lead"), dict) else payload

    try:
        scored = score_lead(lead)
        due = needs_followup(lead, now=datetime.now(timezone.utc), window_hours=followup_hours)
        action = suggest_next_action(scored["quality"], due)
    except Exception as e:
        lightsei.emit("lead.crash", {"command_id": cmd_id, "error": repr(e),
                                     "traceback": traceback.format_exc()}, run_id=run_id)
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"⚠️ lead: crashed scoring a lead ({type(e).__name__})",
                               "severity": "error"}, source_agent="lead")
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    outcome = {
        "command_id": cmd_id,
        "lead": {k: lead.get(k) for k in ("name", "email", "company") if lead.get(k)},
        "score": scored["score"],
        "quality": scored["quality"],
        "reasons": scored["reasons"],
        "needs_followup": due,
        "suggested_action": action,
        "severity": "error" if (scored["quality"] == "hot" and due) else "info",
    }
    lightsei.emit("lead.scored", outcome, run_id=run_id)

    # Surface the leads worth acting on now: hot or warm + due. Cold leads
    # and not-yet-due leads accrue in the event stream without paging.
    if due and scored["quality"] in ("hot", "warm"):
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": hermes_text_for(lead, scored, action),
                               "severity": outcome["severity"]}, source_agent="lead")
        except Exception as e:
            print(f"lead: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "lead")
    if not api_key:
        print("lead: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"lead up: agent={agent_name} channel={HERMES_CHANNEL} followup={int(FOLLOWUP_HOURS)}h", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL, followup_hours=FOLLOWUP_HOURS)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"lead tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
