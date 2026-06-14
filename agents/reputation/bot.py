"""Reputation assistant — watches what customers say.

Part of the AI Business Team (Phase 32.3). Analyzes incoming reviews,
flags negative feedback fast so the owner can respond, and suggests how
to respond. Polls `reputation.check` commands, emits a
`reputation.analyzed` event, and dispatches a `hermes.post` alert on
negative reviews. Heuristic sentiment (rating + keywords), no LLM.

Phase 32.3 scope: one command kind (`reputation.check`), one downstream
dispatch (`hermes.post`, only on negative), two event types
(`reputation.analyzed` + `reputation.crash`).

Env (defaults in parens):
  REPUTATION_POLL_S          seconds between claim attempts (5)
  REPUTATION_HERMES_CHANNEL  channel passed to Hermes (default)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required.

Public surface (for tests):
  analyze_sentiment(text, rating=None) -> {sentiment, score, reasons}
  draft_response_hint(sentiment) -> str
  tick(client, *, hermes_channel=...)
  main()
"""
import os
import uuid
import sys
import time
import traceback
from typing import Any, Optional

import lightsei


def _send_with_source(target_agent, kind, payload, *, source_agent):
    try:
        return lightsei.send_command(target_agent, kind, payload, source_agent=source_agent)
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("REPUTATION_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("REPUTATION_HERMES_CHANNEL", "default")

_NEG_WORDS = (
    "terrible", "awful", "horrible", "worst", "bad", "disappointed",
    "disappointing", "rude", "slow", "never again", "waste", "scam",
    "broken", "dirty", "overpriced", "refund", "avoid", "unprofessional",
    "poor", "angry", "frustrated",
)
_POS_WORDS = (
    "great", "excellent", "amazing", "love", "loved", "best", "wonderful",
    "fantastic", "recommend", "friendly", "helpful", "perfect", "fast",
    "professional", "clean", "awesome", "thank", "happy", "outstanding",
)


# ---------- Pure analysis ---------- #


def analyze_sentiment(text: str, rating: Optional[int] = None) -> dict[str, Any]:
    """Heuristic sentiment from a star rating (primary signal) + keyword
    counts in the text. Returns {sentiment, score, reasons}."""
    score = 0
    reasons: list[str] = []

    if rating is not None:
        try:
            r = int(rating)
        except (ValueError, TypeError):
            r = None
        if r is not None:
            if r <= 2:
                score -= 2
                reasons.append(f"{r}-star rating")
            elif r == 3:
                reasons.append("3-star rating")
            else:
                score += 2
                reasons.append(f"{r}-star rating")

    low = (text or "").lower()
    neg = sum(1 for w in _NEG_WORDS if w in low)
    pos = sum(1 for w in _POS_WORDS if w in low)
    if neg:
        score -= neg
        reasons.append(f"{neg} negative phrase(s)")
    if pos:
        score += pos
        reasons.append(f"{pos} positive phrase(s)")

    sentiment = "negative" if score < 0 else "positive" if score > 0 else "neutral"
    return {"sentiment": sentiment, "score": score, "reasons": reasons}


def draft_response_hint(sentiment: str) -> str:
    if sentiment == "negative":
        return ("Respond promptly and empathetically: acknowledge, apologize, "
                "offer to make it right, and take the details offline.")
    if sentiment == "positive":
        return "Thank them warmly and, if it fits, invite them to share with friends."
    return "Thank them for the feedback and invite a little more detail."


def _author(review: dict[str, Any]) -> str:
    return str(review.get("author") or review.get("name") or "a customer")


def hermes_text_for(review: dict[str, Any], analysis: dict[str, Any]) -> str:
    src = review.get("source")
    where = f" on {src}" if src else ""
    return (
        f"\U0001f6a8 reputation: negative review from {_author(review)}{where} "
        f"(score {analysis['score']}) — respond soon"
    )


# ---------- Bot loop ---------- #


def tick(client: Any, *, hermes_channel: str = "default") -> Optional[dict[str, Any]]:
    cmd = lightsei.claim_command(agent_name="reputation")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "reputation.check":
        lightsei.complete_command(cmd_id, error=f"reputation does not handle kind={kind!r}")
        return cmd

    payload = cmd.get("payload") or {}
    run_id = str(uuid.uuid4())  # explicit run_id: these events fire outside
    # an LLM-call run, and emit() drops events with no run context.
    review = payload.get("review") if isinstance(payload.get("review"), dict) else payload

    try:
        analysis = analyze_sentiment(review.get("text") or review.get("message") or "",
                                     review.get("rating"))
        hint = draft_response_hint(analysis["sentiment"])
    except Exception as e:
        lightsei.emit("reputation.crash", {"command_id": cmd_id, "error": repr(e),
                                           "traceback": traceback.format_exc()}, run_id=run_id)
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": f"⚠️ reputation: crashed analyzing a review ({type(e).__name__})",
                               "severity": "error"}, source_agent="reputation")
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    outcome = {
        "command_id": cmd_id,
        "author": _author(review),
        "source": review.get("source"),
        "rating": review.get("rating"),
        "sentiment": analysis["sentiment"],
        "score": analysis["score"],
        "reasons": analysis["reasons"],
        "response_hint": hint,
        "severity": "error" if analysis["sentiment"] == "negative" else "info",
    }
    lightsei.emit("reputation.analyzed", outcome, run_id=run_id)

    # Page the owner only on negative reviews — those need a fast,
    # human response. Positive/neutral accrue in the event stream.
    if analysis["sentiment"] == "negative":
        try:
            _send_with_source("hermes", "hermes.post",
                              {"channel": hermes_channel,
                               "text": hermes_text_for(review, analysis),
                               "severity": "error"}, source_agent="reputation")
        except Exception as e:
            print(f"reputation: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "reputation")
    if not api_key:
        print("reputation: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"reputation up: agent={agent_name} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"reputation tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
