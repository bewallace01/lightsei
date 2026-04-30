"""Shared helpers across formatters.

Putting the cross-platform message-text math here (relative timestamps,
short summaries, top-N action picking, plan/run links) keeps the per-
platform formatters focused on their native shape rather than re-
deriving the same content. Each formatter still controls its own
visual layout — only the strings come from here.
"""
from datetime import datetime, timezone
from typing import Any


def relative_time(ts: datetime) -> str:
    """Lightsei convention: 'just now' under a minute, then 'Nm ago',
    'Nh ago', 'Nd ago'. Matches what the /polaris dashboard renders so
    a user toggling between Slack and the page sees the same labels."""
    now = datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = max(0, int((now - ts).total_seconds()))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"


def truncate(text: str, limit: int = 280) -> str:
    """Slack messages render best under ~3000 chars per block, Discord
    embeds clamp descriptions at 4096 chars. We're under both ceilings
    everywhere, but the orchestrator's `summary` can run long and
    Polaris's `next_actions[].task` can be paragraph-shaped; clip
    aggressively for the notification view since the deep link to the
    dashboard is always there."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def top_next_actions(payload: dict[str, Any], n: int = 3) -> list[dict[str, str]]:
    """Pluck the first N next_actions entries from a polaris.plan
    payload. Defensive: payload shape may not match the schema (Phase
    7 tested the malformed-payload path); we never raise on missing
    fields."""
    actions = payload.get("next_actions") or []
    out: list[dict[str, str]] = []
    for a in actions[:n]:
        if not isinstance(a, dict):
            continue
        out.append({
            "task": truncate(str(a.get("task", "")), 200),
            "why": truncate(str(a.get("why", "")), 200),
            "blocked_by": str(a.get("blocked_by") or ""),
        })
    return out


def first_violation_summary(payload: dict[str, Any]) -> dict[str, str]:
    """For validation.fail signals: turn the first violation in the
    payload's `validations` array into a single-line summary the
    formatters can render. The dashboard's deep link is the path to
    full violation details; this is the headline."""
    validations = payload.get("validations") or []
    for v in validations:
        if not isinstance(v, dict):
            continue
        if v.get("status") not in ("fail", "error"):
            continue
        violations = v.get("violations") or []
        first = violations[0] if violations else {}
        return {
            "validator": str(v.get("validator", "?")),
            "rule": str(first.get("rule", "?")) if isinstance(first, dict) else "?",
            "matched": str(first.get("matched", "")) if isinstance(first, dict) else "",
            "message": truncate(
                str(first.get("message", "")) if isinstance(first, dict) else "",
                200,
            ),
        }
    return {"validator": "?", "rule": "?", "matched": "", "message": ""}


def run_failed_summary(payload: dict[str, Any]) -> dict[str, str]:
    """run_failed events carry an `error` (or `error_message`) field
    plus an optional traceback. We ship just the error string in
    notifications — full traceback lives on the run-detail page."""
    error = (
        payload.get("error")
        or payload.get("error_message")
        or payload.get("message")
        or ""
    )
    return {"error": truncate(str(error), 400)}
