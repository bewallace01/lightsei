"""Microsoft Teams formatter — Adaptive Card 1.5 wrapped in the Teams
attachments envelope.

Microsoft has been deprecating the older MessageCard format and the
legacy "Office 365 Connector" webhook URL. The supported URL today
comes from the Teams app **Workflows** ("Post to a channel when a
webhook request is received"). The body shape that workflow expects
is the standard Bot Framework attachments envelope with an
Adaptive Card 1.5 inside:

    {
      "type": "message",
      "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "contentUrl": null,
        "content": <Adaptive Card JSON>
      }]
    }

The dashboard's "Add channel" form will point users at the Workflows
URL specifically — the legacy URLs no longer accept POSTs.

Adaptive Card spec: https://adaptivecards.io/explorer/
"""
from typing import Any

from ._http import post_json
from ._shared import (
    first_violation_summary,
    relative_time,
    run_failed_summary,
    top_next_actions,
    truncate,
)
from ._types import Delivery, Signal


def format(signal: Signal) -> dict[str, Any]:
    if signal.trigger == "polaris.plan":
        card = _card_plan(signal)
    elif signal.trigger == "validation.fail":
        card = _card_validation_fail(signal)
    elif signal.trigger == "run_failed":
        card = _card_run_failed(signal)
    elif signal.trigger == "test":
        card = _card_test(signal)
    else:
        card = _card_generic(signal)

    return _wrap_attachment(card)


def post(*, url: str, body: dict[str, Any], secret_token: str | None = None) -> Delivery:
    del secret_token  # explicit: documented unused
    return post_json(url=url, body=body)


# ---------- envelope ---------- #


def _wrap_attachment(card: dict[str, Any]) -> dict[str, Any]:
    """Bot Framework `message` envelope. Required by the Teams
    Workflows webhook trigger."""
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": card,
        }],
    }


def _card_base() -> dict[str, Any]:
    return {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.5",
        "body": [],
    }


# ---------- per-trigger card builders ---------- #


def _card_plan(signal: Signal) -> dict[str, Any]:
    payload = signal.payload
    summary = truncate(str(payload.get("summary") or "(no summary in payload)"), 600)
    actions = top_next_actions(payload, n=3)

    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": "🌟 Polaris plan", "weight": "Bolder", "size": "Large", "wrap": True},
        {"type": "TextBlock", "text": f"{signal.agent_name} · {relative_time(signal.timestamp)}", "isSubtle": True, "spacing": "None", "wrap": True},
        {"type": "TextBlock", "text": summary, "wrap": True, "spacing": "Medium"},
    ]
    if actions:
        body.append({"type": "TextBlock", "text": "Next actions", "weight": "Bolder", "spacing": "Medium", "wrap": True})
        for i, a in enumerate(actions, 1):
            body.append({"type": "TextBlock", "text": f"**{i}.** {a['task']}", "wrap": True, "spacing": "Small"})
            if a.get("blocked_by"):
                body.append({"type": "TextBlock", "text": f"_blocked by: {a['blocked_by']}_", "isSubtle": True, "spacing": "None", "wrap": True})

    card = _card_base()
    card["body"] = body
    card["actions"] = [{"type": "Action.OpenUrl", "title": "View full plan", "url": signal.dashboard_url}]
    return card


def _card_validation_fail(signal: Signal) -> dict[str, Any]:
    v = first_violation_summary(signal.payload)
    facts = [
        {"title": "Validator", "value": v["validator"]},
        {"title": "Rule", "value": v["rule"]},
    ]
    if v.get("matched"):
        facts.append({"title": "Matched", "value": v["matched"]})

    card = _card_base()
    card["body"] = [
        {"type": "TextBlock", "text": f"🔴 Validation failed: {signal.agent_name}", "weight": "Bolder", "size": "Large", "color": "Attention", "wrap": True},
        {"type": "FactSet", "facts": facts, "spacing": "Medium"},
        {"type": "TextBlock", "text": v["message"] or "(no message)", "wrap": True, "spacing": "Medium"},
    ]
    card["actions"] = [{"type": "Action.OpenUrl", "title": "View plan", "url": signal.dashboard_url}]
    return card


def _card_run_failed(signal: Signal) -> dict[str, Any]:
    info = run_failed_summary(signal.payload)
    error = info["error"] or "(no error message in payload)"
    card = _card_base()
    card["body"] = [
        {"type": "TextBlock", "text": f"💥 {signal.agent_name} run failed", "weight": "Bolder", "size": "Large", "color": "Attention", "wrap": True},
        # Adaptive Cards don't have a true code-block, but `fontType:
        # Monospace` + `wrap: true` renders a fixed-width error string
        # readably across desktop / mobile / web Teams clients.
        {"type": "TextBlock", "text": error, "wrap": True, "fontType": "Monospace", "spacing": "Medium"},
    ]
    card["actions"] = [{"type": "Action.OpenUrl", "title": "View run", "url": signal.dashboard_url}]
    return card


def _card_test(signal: Signal) -> dict[str, Any]:
    card = _card_base()
    card["body"] = [
        {"type": "TextBlock", "text": "✅ Lightsei test message", "weight": "Bolder", "size": "Large", "color": "Good", "wrap": True},
        {"type": "TextBlock", "text": (
            f"If you're seeing this, your Teams channel is wired up. Real notifications "
            f"for **{signal.agent_name}** will arrive when their triggers fire."
        ), "wrap": True, "spacing": "Medium"},
    ]
    card["actions"] = [{"type": "Action.OpenUrl", "title": "Manage channels", "url": signal.dashboard_url}]
    return card


def _card_generic(signal: Signal) -> dict[str, Any]:
    card = _card_base()
    card["body"] = [
        {"type": "TextBlock", "text": f"Lightsei {signal.trigger}", "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": signal.agent_name, "isSubtle": True, "spacing": "None", "wrap": True},
    ]
    card["actions"] = [{"type": "Action.OpenUrl", "title": "View in dashboard", "url": signal.dashboard_url}]
    return card
