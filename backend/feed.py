"""Phase 34.2: the proactive feed.

A unified, owner-facing timeline of what the AI business team did: digests,
review flags, lead scores, triaged emails, marketing drafts, website
checks, plus errors. Each persona already emits an event with a `severity`
field ("error" for the notable cases); this module maps those raw events
into human-readable feed items.

`build_feed_item` is pure (event in, item out or None) so the mapping is
unit-testable without a database. The endpoint just queries recent events
and runs them through it.
"""
from __future__ import annotations

from typing import Any, Optional

# The non-crash events worth surfacing, and the crash events (always an
# alert). Anything else (internal/among-bot chatter) is dropped.
_FEED_KINDS = {
    "reputation.analyzed",
    "lead.scored",
    "inbox.processed",
    "website.check_complete",
    "marketing.created",
    "bi.summary",
}
_CRASH_KINDS = {
    "reputation.crash", "lead.crash", "inbox.crash",
    "website.crash", "marketing.crash", "bi.crash",
}

# Persona agent name -> the business-friendly label the owner sees.
ASSISTANT_LABELS = {
    "bi": "Business Intelligence",
    "reputation": "Reputation",
    "lead": "Lead Management",
    "inbox": "Inbox",
    "marketing": "Marketing",
    "website": "Website",
}

# All kinds this module knows how to render (for the endpoint's WHERE).
FEED_EVENT_KINDS = sorted(_FEED_KINDS | _CRASH_KINDS)


def _truncate(text: Optional[str], n: int = 140) -> Optional[str]:
    if not text:
        return None
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def build_feed_item(event: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Map one raw event to a feed item, or None if it isn't feed-worthy.

    Item shape: {id, assistant, assistant_label, kind, title, detail,
    severity, timestamp}. `severity` is "alert" (needs attention) or
    "info". Reads each persona's own `severity` field ("error" -> alert).
    """
    kind = event.get("kind") or ""
    agent = event.get("agent_name") or "unknown"
    payload = event.get("payload") or {}
    label = ASSISTANT_LABELS.get(agent, agent.title())

    base = {
        "id": event.get("id"),
        "assistant": agent,
        "assistant_label": label,
        "kind": kind,
        "timestamp": event.get("timestamp"),
    }

    if kind in _CRASH_KINDS:
        return {
            **base,
            "title": f"{label} hit an error",
            "detail": _truncate(payload.get("error")),
            "severity": "alert",
        }

    if kind not in _FEED_KINDS:
        return None

    alert = payload.get("severity") == "error"
    title, detail = _render(kind, payload)
    return {**base, "title": title, "detail": detail,
            "severity": "alert" if alert else "info"}


def _render(kind: str, p: dict[str, Any]) -> tuple[str, Optional[str]]:
    if kind == "reputation.analyzed":
        sentiment = str(p.get("sentiment") or "new")
        author = p.get("author") or "a customer"
        rating = p.get("rating")
        title = f"New {sentiment} review from {author}"
        detail = f"{rating}-star" if rating is not None else p.get("source")
        return title, _truncate(detail)

    if kind == "lead.scored":
        score = p.get("score")
        quality = p.get("quality") or "lead"
        lead = p.get("lead") or {}
        who = lead.get("name") or lead.get("company") or lead.get("email")
        title = f"Lead scored {score} ({quality})" if score is not None \
            else f"Lead marked {quality}"
        return title, _truncate(p.get("suggested_action") or who)

    if kind == "inbox.processed":
        category = p.get("category") or "email"
        flag = " — needs you" if (p.get("urgency") == "high"
                                  or p.get("needs_human")) else ""
        title = f"Email triaged: {category}{flag}"
        return title, _truncate(p.get("subject") or p.get("summary"))

    if kind == "website.check_complete":
        up = p.get("up")
        broken = len(p.get("broken_links") or [])
        if up is False:
            title = "Website looks down"
        elif broken:
            title = f"Website check: {broken} broken link(s)"
        else:
            title = "Website checked: all good"
        return title, _truncate(p.get("url"))

    if kind == "marketing.created":
        task = str(p.get("task") or "content").replace("_", " ")
        return f"Marketing {task} draft ready", _truncate(p.get("content"))

    if kind == "bi.summary":
        is_answer = p.get("kind") == "answer"
        title = "Answered a question" if is_answer else "Business summary ready"
        return title, _truncate(p.get("summary"))

    return kind, None
