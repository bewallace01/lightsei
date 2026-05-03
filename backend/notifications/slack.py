"""Slack formatter — Block Kit JSON for incoming webhooks.

Slack incoming webhooks (https://api.slack.com/messaging/webhooks)
accept a JSON body with `text` (fallback for notifications + screen
readers) and `blocks` (the rendered layout). We always set both so a
phone preview shows something sensible even if the receiver client
doesn't render the blocks fully.

Three message templates: polaris.plan, validation.fail, run_failed.
The mattermost channel type re-uses these formatters since Mattermost
accepts the same payload shape.
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
    """Dispatch on trigger and return the Slack webhook JSON body."""
    if signal.trigger == "polaris.plan":
        return _format_plan(signal)
    if signal.trigger == "validation.fail":
        return _format_validation_fail(signal)
    if signal.trigger == "run_failed":
        return _format_run_failed(signal)
    if signal.trigger == "test":
        return _format_test(signal)
    if signal.trigger == "hermes.post":
        return _format_hermes_post(signal)
    # Future-proof: an unrecognized trigger lands a generic message
    # rather than raising. Keeps a Phase 10+ trigger from breaking
    # already-deployed Slack channels until the formatter is updated.
    return _format_generic(signal)


def post(*, url: str, body: dict[str, Any], secret_token: str | None = None) -> Delivery:
    """Slack incoming webhooks ignore extra headers; secret_token isn't
    used here (the URL is itself the credential)."""
    del secret_token  # explicit: documented unused
    return post_json(url=url, body=body)


# ---------- per-trigger formatters ---------- #


def _format_plan(signal: Signal) -> dict[str, Any]:
    payload = signal.payload
    summary = truncate(str(payload.get("summary") or "(no summary in payload)"), 500)
    actions = top_next_actions(payload, n=3)

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": "🌟 Polaris plan", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{signal.agent_name}* · {relative_time(signal.timestamp)}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]
    if actions:
        action_lines = []
        for i, a in enumerate(actions, 1):
            line = f"*{i}.* {a['task']}"
            if a.get("blocked_by"):
                line += f"\n   _blocked by: {a['blocked_by']}_"
            action_lines.append(line)
        blocks.extend([
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Next actions*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n\n".join(action_lines)}},
        ])
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"<{signal.dashboard_url}|View full plan ↗>"},
        ],
    })

    return {
        "text": f"Polaris plan · {signal.agent_name} · {summary[:80]}",
        "blocks": blocks,
    }


def _format_validation_fail(signal: Signal) -> dict[str, Any]:
    v = first_violation_summary(signal.payload)
    fallback = (
        f"Validation failed on {signal.agent_name}: "
        f"{v['validator']}/{v['rule']} — {v['message']}"
    )
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"🔴 Validation failed: {signal.agent_name}", "emoji": True}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Validator*\n`{v['validator']}`"},
            {"type": "mrkdwn", "text": f"*Rule*\n`{v['rule']}`"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": v["message"] or "_(no message)_"}},
    ]
    if v.get("matched"):
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_matched_ `{v['matched']}`"}],
        })
    blocks.append({
        "type": "context",
        "elements": [
            {"type": "mrkdwn", "text": f"<{signal.dashboard_url}|View plan ↗>"},
        ],
    })
    return {"text": fallback, "blocks": blocks}


def _format_run_failed(signal: Signal) -> dict[str, Any]:
    info = run_failed_summary(signal.payload)
    error = info["error"] or "_(no error message in payload)_"
    fallback = f"{signal.agent_name} run failed: {info['error'][:120]}"
    return {
        "text": fallback,
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"💥 {signal.agent_name} run failed", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Error*\n```\n{error}\n```"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"<{signal.dashboard_url}|View run ↗>"},
            ]},
        ],
    }


def _format_test(signal: Signal) -> dict[str, Any]:
    return {
        "text": f"Lightsei test message ({signal.agent_name})",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "✅ Lightsei test message", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                "If you're seeing this, your Slack channel is wired up. "
                f"Real notifications for *{signal.agent_name}* will arrive when "
                "their triggers fire."
            )}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"<{signal.dashboard_url}|Manage channels ↗>"},
            ]},
        ],
    }


def _format_hermes_post(signal: Signal) -> dict[str, Any]:
    """Phase 11.4: Hermes is the workspace's notifier bot. Upstream
    agents (Atlas's tests_run summary, future agents' alerts) hand
    Hermes a fully-formed `text` line — Hermes does no per-channel
    formatting beyond wrapping it in a Slack section block. Keeps
    the agent ↔ channel coupling thin: the upstream agent decides
    what to say, the formatter only decides how to render it on
    each platform.

    payload shape:
        text:     str — the message body, already including any
                  emoji prefix the upstream agent wants.
        severity: 'info' | 'error' (optional, default 'info'). Drives
                  the header color; the body text is unchanged.
    """
    text = str(signal.payload.get("text") or "(empty hermes.post)")
    return {
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
        ],
    }


def _format_generic(signal: Signal) -> dict[str, Any]:
    return {
        "text": f"Lightsei {signal.trigger} on {signal.agent_name}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": (
                f"*{signal.trigger}* on `{signal.agent_name}` — "
                f"<{signal.dashboard_url}|view in dashboard>"
            )}},
        ],
    }
