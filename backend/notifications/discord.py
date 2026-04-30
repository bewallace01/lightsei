"""Discord formatter — embed objects for incoming webhooks.

Discord webhooks (https://discord.com/developers/docs/resources/webhook
#execute-webhook) accept a JSON body with `content` (plain text) and
`embeds` (richer rendering). We render the same three trigger
templates as Slack, but using Discord's native `embed` shape — title,
description, fields, footer, color.

Color coding: green for plan (steady state), amber for warn/test (soft
signal), red for fail/run_failed (alarm). Discord users expect color
as the visual cue, where Slack users get emoji.
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

# Discord embed colors are 24-bit integers. Picking the same hex values
# the dashboard's `STATUS_STYLES` chips use so a user toggling between
# Discord and the page sees the same palette.
COLOR_GREEN = 0x10B981   # emerald-500
COLOR_AMBER = 0xF59E0B   # amber-500
COLOR_RED   = 0xEF4444   # red-500


def format(signal: Signal) -> dict[str, Any]:
    if signal.trigger == "polaris.plan":
        return _format_plan(signal)
    if signal.trigger == "validation.fail":
        return _format_validation_fail(signal)
    if signal.trigger == "run_failed":
        return _format_run_failed(signal)
    if signal.trigger == "test":
        return _format_test(signal)
    return _format_generic(signal)


def post(*, url: str, body: dict[str, Any], secret_token: str | None = None) -> Delivery:
    del secret_token  # explicit: documented unused
    return post_json(url=url, body=body)


# ---------- per-trigger formatters ---------- #


def _format_plan(signal: Signal) -> dict[str, Any]:
    payload = signal.payload
    summary = truncate(str(payload.get("summary") or "(no summary in payload)"), 1000)
    actions = top_next_actions(payload, n=3)

    description_parts = [summary]
    if actions:
        description_parts.append("")  # blank line before list
        for i, a in enumerate(actions, 1):
            line = f"**{i}.** {a['task']}"
            if a.get("blocked_by"):
                line += f"\n*blocked by: {a['blocked_by']}*"
            description_parts.append(line)

    return {
        "embeds": [{
            "title": f"🌟 Polaris plan · {signal.agent_name}",
            "description": "\n\n".join(description_parts),
            "color": COLOR_GREEN,
            "url": signal.dashboard_url,
            "footer": {"text": f"Generated {relative_time(signal.timestamp)} • View full plan in dashboard"},
            "timestamp": signal.timestamp.isoformat(),
        }],
    }


def _format_validation_fail(signal: Signal) -> dict[str, Any]:
    v = first_violation_summary(signal.payload)
    fields = [
        {"name": "Validator", "value": f"`{v['validator']}`", "inline": True},
        {"name": "Rule", "value": f"`{v['rule']}`", "inline": True},
    ]
    if v.get("matched"):
        fields.append({"name": "Matched", "value": f"`{v['matched']}`", "inline": False})
    return {
        "embeds": [{
            "title": f"🔴 Validation failed: {signal.agent_name}",
            "description": v["message"] or "_(no message)_",
            "color": COLOR_RED,
            "url": signal.dashboard_url,
            "fields": fields,
            "footer": {"text": "View plan in dashboard"},
            "timestamp": signal.timestamp.isoformat(),
        }],
    }


def _format_run_failed(signal: Signal) -> dict[str, Any]:
    info = run_failed_summary(signal.payload)
    error = info["error"] or "*(no error message in payload)*"
    return {
        "embeds": [{
            "title": f"💥 {signal.agent_name} run failed",
            "description": f"```\n{error}\n```",
            "color": COLOR_RED,
            "url": signal.dashboard_url,
            "footer": {"text": "View run in dashboard"},
            "timestamp": signal.timestamp.isoformat(),
        }],
    }


def _format_test(signal: Signal) -> dict[str, Any]:
    return {
        "embeds": [{
            "title": "✅ Lightsei test message",
            "description": (
                "If you're seeing this, your Discord channel is wired up. "
                f"Real notifications for **{signal.agent_name}** will arrive "
                "when their triggers fire."
            ),
            "color": COLOR_AMBER,
            "url": signal.dashboard_url,
            "footer": {"text": "Manage channels in dashboard"},
            "timestamp": signal.timestamp.isoformat(),
        }],
    }


def _format_generic(signal: Signal) -> dict[str, Any]:
    return {
        "embeds": [{
            "title": f"Lightsei {signal.trigger}",
            "description": f"`{signal.agent_name}` — view in dashboard",
            "color": COLOR_AMBER,
            "url": signal.dashboard_url,
            "timestamp": signal.timestamp.isoformat(),
        }],
    }
