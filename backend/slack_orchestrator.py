"""Phase 19.4: chat orchestrator handler.

Handles `kind='slack_orchestration'` jobs from the generation_jobs queue
(enqueued by the 19.3 events webhook on `app_mention`). The flow:

1. Look up the channel from slack_channels. Auto-discover (insert a row
   with default sensitivity_level='internal', opted_in=false) on first
   sight; post a "this channel isn't connected" nudge back to Slack and
   return — the operator opts the channel in from the dashboard.

2. If opted in, find all agents in the Lightsei workspace whose
   sensitivity_level matches the channel's. The trust-zone wedge applied
   to the chat boundary: a #internal-finance channel (sensitivity='pii')
   can only reach pii-tagged bots.

3. Filter further to bots that have the `slack:respond` capability
   (granted by the Compliance preset's internal + public hint mappings;
   absent on pii + sensitive bots). The capability check is the same
   default-deny pattern as Phase 16.3 — a bot can't respond to Slack
   unless an operator explicitly grants it.

4. If no candidates, post a friendly "no bots are available for this
   zone in this channel" back to Slack.

5. With candidates, call Anthropic with a routing prompt: given the
   request text + the candidate roster, which bot handles this? The LLM
   returns `{target_agent, why}` via a tool-use schema.

6. Dispatch a `slack.respond` command (via the existing Phase 11.2
   command queue) to the chosen bot with the message payload. The bot's
   handler runs whatever logic + calls `lightsei.post_slack` (19.5) to
   reply in-thread.

LLM calls are mocked-out in tests via the same Anthropic-stub pattern
used by team_planner.py + agent_generator.py.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("lightsei.slack_orchestrator")


# Capability bots need in order to be routed-to from Slack. Mirrors the
# Phase 16.3 capability-gate semantics: if a bot doesn't have this, the
# orchestrator never picks it. Granted by Compliance preset's internal
# + public hint mappings (P16.x); absent on pii + sensitive bots by
# default, so PII bots can't even RECEIVE chat commands from Slack.
SLACK_RESPOND_CAPABILITY = "slack:respond"


# Anthropic tool the routing call uses. Strict mode + a single required
# field so the LLM has minimal room to drift.
ROUTING_TOOL = {
    "name": "pick_bot",
    "description": (
        "Pick the bot that should handle this Slack mention, based on "
        "the request text + the candidate roster. Return the bot's "
        "exact name from the candidates list."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "target_agent": {
                "type": "string",
                "description": (
                    "The name of the chosen bot. Must exactly match one "
                    "of the candidate names — case-sensitive."
                ),
            },
            "why": {
                "type": "string",
                "description": (
                    "One sentence on why this bot fits. Surfaced to the "
                    "operator for audit; the user doesn't see it."
                ),
            },
        },
        "required": ["target_agent", "why"],
        "additionalProperties": False,
    },
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _channel_not_connected_message() -> str:
    return (
        "Hi! I'm not configured for this channel yet. An admin can "
        "connect it from <https://app.lightsei.com/integrations/slack|"
        "the Lightsei dashboard>."
    )


def _no_candidates_message(channel_sensitivity: str) -> str:
    return (
        f"No bots are available to handle this in the {channel_sensitivity!r} "
        "zone. An admin can configure bots for this channel from "
        "<https://app.lightsei.com/agents|the agents page>."
    )


def _routing_failed_message() -> str:
    return (
        "I couldn't figure out which bot should handle this. "
        "Try rephrasing or pinging a specific bot by name."
    )


def _build_routing_user_message(
    *, mention_text: str, candidates: list[dict[str, Any]],
) -> str:
    """Pack the routing-call user message: the user's request + a
    bulleted list of candidate bots with their summaries."""
    bullets = "\n".join(
        f"- `{c['name']}`: {c.get('summary') or '(no summary)'}"
        for c in candidates
    )
    return (
        "A Slack user just mentioned the Lightsei app. Their message:\n\n"
        f"> {mention_text}\n\n"
        "These bots are available in this channel's zone:\n\n"
        f"{bullets}\n\n"
        "Pick the bot whose summary best matches what the user wants. "
        "Call `pick_bot` with the chosen bot's exact name. If none fit, "
        "pick the one with the closest match — don't refuse to route."
    )


def _build_routing_system_message(channel_sensitivity: str) -> str:
    return (
        "You're the chat-router for Lightsei, a configure-your-team AI "
        "agent platform. A user just mentioned the Lightsei Slack app "
        f"in a channel scoped to the {channel_sensitivity!r} trust zone. "
        "Only bots in that zone are reachable from this channel; the "
        "candidates below have already been filtered. Pick the one whose "
        "summary best matches the user's intent."
    )


def run_slack_orchestration_job(
    session,
    workspace_id: str,
    payload: dict,
) -> dict:
    """Job-runner handler for `kind='slack_orchestration'`.

    Returns a dict the generation_jobs row stores as `result_payload`.
    Always returns (never raises), since orchestration failures are
    routine operational state — the user-visible response is whatever
    we manage to post back to Slack.
    """
    # Local imports keep test-time import surface narrow + avoid the
    # main → models → handler → main circularity that's bitten us
    # before.
    import anthropic
    from sqlalchemy import select

    import slack_client
    from models import (
        Agent,
        Command,
        SlackChannel,
        WorkspaceSecret,
    )
    import secrets_crypto

    slack_team_id = payload.get("slack_team_id")
    channel_id = payload.get("channel_id")
    user_id = payload.get("user_id")
    text = (payload.get("text") or "").strip()
    thread_ts = payload.get("thread_ts") or payload.get("ts")

    if not slack_team_id or not channel_id:
        return {"status": "skipped", "reason": "missing_slack_ids"}

    # ---- 1. Find or auto-discover the channel. ---- #
    channel = session.get(SlackChannel, (slack_team_id, channel_id))
    if channel is None:
        # First time we've seen this channel. Insert with safe defaults
        # — silent, internal zone — so the operator can opt it in from
        # the dashboard.
        channel = SlackChannel(
            slack_team_id=slack_team_id,
            channel_id=channel_id,
            lightsei_workspace_id=workspace_id,
            channel_name=channel_id,  # name-fetch deferred to a later sub-task
            sensitivity_level="internal",
            opted_in=False,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        session.add(channel)
        session.flush()

    # ---- 2. Opt-in gate. ---- #
    if not channel.opted_in:
        _try_post_to_slack(
            session, slack_team_id, channel_id,
            _channel_not_connected_message(),
            thread_ts=thread_ts,
        )
        return {
            "status": "skipped",
            "reason": "channel_not_opted_in",
            "channel_id": channel_id,
        }

    # ---- 3. Candidate roster: zone match + slack:respond capability. ---- #
    zone = channel.sensitivity_level
    candidate_rows = session.execute(
        select(Agent).where(
            Agent.workspace_id == workspace_id,
            Agent.sensitivity_level == zone,
        ).order_by(Agent.name)
    ).scalars().all()
    candidates = [
        {
            "name": a.name,
            "summary": (a.description or "").splitlines()[0]
            if a.description else "",
        }
        for a in candidate_rows
        if not a.name.startswith("lightsei.")  # skip workspace-internal
        and SLACK_RESPOND_CAPABILITY in (a.capabilities or [])
    ]

    if not candidates:
        _try_post_to_slack(
            session, slack_team_id, channel_id,
            _no_candidates_message(zone),
            thread_ts=thread_ts,
        )
        return {
            "status": "skipped",
            "reason": "no_candidates",
            "zone": zone,
        }

    # ---- 4. Anthropic routing call. ---- #
    secret_row = session.get(WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY"))
    if secret_row is None:
        _try_post_to_slack(
            session, slack_team_id, channel_id,
            "Lightsei isn't fully configured for this workspace yet "
            "(missing Anthropic API key). An admin can set it from "
            "<https://app.lightsei.com/account|/account>.",
            thread_ts=thread_ts,
        )
        return {"status": "skipped", "reason": "missing_anthropic_key"}

    try:
        anthropic_key = secrets_crypto.decrypt(secret_row.encrypted_value)
    except Exception:
        return {"status": "failed", "reason": "decrypt_anthropic_key"}

    # Close the read transaction before the LLM call — Railway Postgres
    # kills idle-in-transaction connections during multi-second waits.
    # Same pattern as team_planner / agent_generator (see commits
    # f218fd3 + 9e65358).
    session.commit()

    client = anthropic.Anthropic(api_key=anthropic_key, max_retries=3)
    try:
        resp = client.messages.create(
            model="claude-opus-4-7",
            max_tokens=400,
            system=_build_routing_system_message(zone),
            tools=[ROUTING_TOOL],
            tool_choice={"type": "tool", "name": "pick_bot"},
            messages=[
                {"role": "user", "content": _build_routing_user_message(
                    mention_text=text, candidates=candidates,
                )},
            ],
        )
    except Exception as exc:
        logger.exception("slack_orchestrator: routing call failed")
        _try_post_to_slack(
            session, slack_team_id, channel_id,
            _routing_failed_message(),
            thread_ts=thread_ts,
        )
        return {"status": "failed", "reason": "anthropic_error", "error": str(exc)}

    chosen = _extract_routing_choice(resp)
    if chosen is None or chosen["target_agent"] not in {c["name"] for c in candidates}:
        _try_post_to_slack(
            session, slack_team_id, channel_id,
            _routing_failed_message(),
            thread_ts=thread_ts,
        )
        return {"status": "failed", "reason": "router_picked_unknown_bot"}

    # ---- 5. Dispatch slack.respond command to the chosen bot. ---- #
    cmd_id = str(uuid.uuid4())
    session.add(Command(
        id=cmd_id,
        workspace_id=workspace_id,
        agent_name=chosen["target_agent"],
        kind="slack.respond",
        payload={
            "text": text,
            "channel_id": channel_id,
            "slack_team_id": slack_team_id,
            "user_id": user_id,
            "thread_ts": thread_ts,
            "slack_event_id": payload.get("slack_event_id"),
        },
        status="pending",
        approval_state="approved",  # operator opt-in on the channel is the approval
        created_at=_utcnow(),
        expires_at=_utcnow().replace(microsecond=0) + _command_ttl(),
    ))
    session.flush()

    return {
        "status": "dispatched",
        "target_agent": chosen["target_agent"],
        "why": chosen["why"],
        "command_id": cmd_id,
        "zone": zone,
    }


def _extract_routing_choice(resp: Any) -> Optional[dict[str, str]]:
    """Pull the pick_bot tool-use block out of the response. Returns
    None if the LLM didn't call the tool (would be a bug given strict
    tool_choice; defensive)."""
    block = next(
        (
            b for b in resp.content
            if getattr(b, "type", None) == "tool_use"
            and getattr(b, "name", None) == "pick_bot"
        ),
        None,
    )
    if block is None:
        return None
    return {
        "target_agent": str(block.input.get("target_agent") or ""),
        "why": str(block.input.get("why") or ""),
    }


def _try_post_to_slack(
    session,
    slack_team_id: str,
    channel: str,
    text: str,
    thread_ts: Optional[str] = None,
) -> None:
    """Best-effort Slack post for error paths. If posting fails, log +
    swallow — we don't want to fail the orchestration job because we
    couldn't post a nudge."""
    import slack_client
    try:
        slack_client.post_message(
            session=session,
            slack_team_id=slack_team_id,
            channel=channel,
            text=text,
            thread_ts=thread_ts,
        )
    except slack_client.SlackClientError as exc:
        logger.warning(
            "slack_orchestrator: nudge post failed for team=%s channel=%s: %s",
            slack_team_id, channel, exc,
        )


def _command_ttl():
    """Commands the orchestrator queues for slack.respond expire after
    24 hours. Match the default Command.expires_at TTL used elsewhere
    in the codebase."""
    from datetime import timedelta
    return timedelta(hours=24)


def _register() -> None:
    import jobs
    jobs.register_handler("slack_orchestration", run_slack_orchestration_job)


_register()
