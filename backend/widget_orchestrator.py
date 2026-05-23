"""Phase 21.6: widget chat job handler.

Picks up `widget_chat` jobs enqueued by 21.2's POST
/widget/{public_id}/messages endpoint. For each, finds the
workspace's customer-facing bot + enqueues a `widget.chat` Command
on it with the conversation history. The bot's command poller
(SDK side) picks the Command up + dispatches to the user's
`@on_chat("widget")` handler via a built-in bridge handler the
SDK ships in Phase 21.5 / 21.6.

Parallel to `slack_orchestrator.py` from Phase 19.4 — same
shape: orchestrator on the backend finds the bot, the actual
LLM call happens in the bot's deployed process via Command
dispatch. Keeps the backend stateless w.r.t. LLM provider
selection + lets the bot author put real Python around the
handler (call connectors, redact PII, branch on payload, etc.).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select


logger = logging.getLogger("lightsei.widget_orchestrator")


# Capability required on the bot to receive widget.chat commands.
WIDGET_RESPOND_CAPABILITY = "widget:respond"

# How much conversation context to ship to the bot per turn. Big
# enough to keep the bot grounded in the thread; small enough that
# a long conversation doesn't blow up the LLM context window. The
# bot can fetch more via the poll endpoint if it wants.
WIDGET_HISTORY_LIMIT = 20

# Lifetime of the widget.chat Command rows. Bot has 24h to pick up
# the command; after that the row expires + the next message will
# re-enqueue. Same default as the slack.respond TTL from 19.4.
WIDGET_COMMAND_TTL = timedelta(hours=24)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def run_widget_chat_job(
    session,
    workspace_id: str,
    payload: dict,
) -> dict:
    """Job-runner handler for `kind='widget_chat'`.

    Always returns a dict (never raises) — orchestration failures
    are routine operational state and the user-visible response is
    whatever lands in the conversation thread (system message, bot
    message, or eventual escalation).

    Pipeline:

      1. Load the conversation + new user message. Bail if the
         conversation has been deleted between enqueue + run.
      2. Look up the workspace's customer-facing bot (snapshotted
         on the conversation row at start-time so renaming the
         workspace's `customer_facing_agent_name` mid-thread
         doesn't break in-flight conversations).
      3. If the bot row was deleted, persist a system message + stop.
      4. Capability check (defense-in-depth — the /widget-bot/respond
         endpoint also enforces). Missing widget:respond → persist
         a bot-role message explaining the bot isn't set up + stop.
      5. Pull the conversation's last WIDGET_HISTORY_LIMIT messages
         for the bot to use as context.
      6. Enqueue a `widget.chat` Command on the bot. The SDK's
         built-in bridge handler (21.6) picks it up + calls the
         user-registered `@on_chat("widget")` handler.
    """
    from models import Agent, Command, WidgetConversation, WidgetMessage

    conversation_id = payload.get("conversation_id")
    user_message_id = payload.get("user_message_id")
    if not conversation_id:
        return {"status": "skipped", "reason": "missing_conversation_id"}

    conv = session.get(WidgetConversation, conversation_id)
    if conv is None or conv.workspace_id != workspace_id:
        return {"status": "skipped", "reason": "conversation_not_found"}

    # operator_owned + resolved conversations shouldn't have gotten
    # here — 21.2's POST endpoint skips enqueueing in those states —
    # but defense in depth: if it did somehow, no-op.
    if conv.status in ("resolved", "operator_owned"):
        return {
            "status": "skipped",
            "reason": "conversation_not_open",
            "conversation_status": conv.status,
        }

    bot_name = conv.customer_facing_agent_name
    bot = session.get(Agent, (workspace_id, bot_name))

    if bot is None:
        # Operator picked a bot, then deleted the agent before the
        # bot replied. Drop a system message so the end user sees
        # SOMETHING in the conversation, then leave the conversation
        # open — the operator can take it over from /inbox.
        _persist_system_message(
            session, conv,
            "No customer-facing bot is configured yet — please contact "
            "the operator.",
        )
        return {"status": "failed", "reason": "bot_not_found"}

    if WIDGET_RESPOND_CAPABILITY not in (bot.capabilities or []):
        # Operator wired the bot up but didn't grant the capability.
        # Surface a bot-role message that's actionable (vs. a silent
        # failure where the end user thinks they were ignored).
        _persist_bot_message(
            session, conv,
            "This bot isn't set up to answer chat yet — please contact "
            "the operator.",
        )
        return {
            "status": "skipped",
            "reason": "missing_widget_respond_capability",
        }

    # Load the conversation history. Newest WIDGET_HISTORY_LIMIT,
    # then reverse to chronological. Includes the new user message
    # we're responding to — the bridge handler pops it off as the
    # `user_message` and passes the rest as `conversation_history`.
    history_rows = session.execute(
        select(WidgetMessage)
        .where(WidgetMessage.conversation_id == conv.id)
        .order_by(WidgetMessage.id.desc())
        .limit(WIDGET_HISTORY_LIMIT)
    ).scalars().all()
    history_rows.reverse()

    # Pluck the new user message out so the bridge handler can pass
    # it as `user_message` separately from `conversation_history`.
    user_message_text = ""
    if user_message_id:
        for m in history_rows:
            if m.id == user_message_id:
                user_message_text = m.text
                break
    if not user_message_text:
        # Fallback: last user message in the history. Covers the
        # case where the job picked up before we recorded the
        # user_message_id (shouldn't happen, defense-in-depth).
        for m in reversed(history_rows):
            if m.role == "user":
                user_message_text = m.text
                break

    cmd_id = str(uuid.uuid4())
    session.add(Command(
        id=cmd_id,
        workspace_id=workspace_id,
        agent_name=bot.name,
        kind="widget.chat",
        payload={
            "conversation_id": conv.id,
            "user_message": user_message_text,
            "conversation_history": [
                {"role": m.role, "text": m.text}
                for m in history_rows
                if not (user_message_id and m.id == user_message_id)
            ],
        },
        status="pending",
        approval_state="approved",  # operator wiring the bot up is the approval
        created_at=_utcnow(),
        expires_at=_utcnow() + WIDGET_COMMAND_TTL,
    ))
    session.flush()

    return {
        "status": "dispatched",
        "target_agent": bot.name,
        "command_id": cmd_id,
        "conversation_id": conv.id,
        "history_count": len(history_rows),
    }


def _persist_system_message(session, conv, text: str) -> None:
    """Drop a system-role message into the conversation thread +
    bump last_message_at. Used by the orchestrator's error paths so
    the end user sees something in the iframe rather than silence."""
    from models import WidgetMessage

    now = _utcnow()
    session.add(WidgetMessage(
        conversation_id=conv.id,
        role="system",
        text=text,
        sent_at=now,
    ))
    conv.last_message_at = now
    session.flush()


def _persist_bot_message(session, conv, text: str) -> None:
    """Drop a bot-role message. For error paths where the bot
    isn't set up but the failure mode reads more naturally as
    "the bot said it can't help" than "system: bot misconfigured."
    """
    from models import WidgetMessage

    now = _utcnow()
    session.add(WidgetMessage(
        conversation_id=conv.id,
        role="bot",
        text=text,
        sent_at=now,
    ))
    conv.last_message_at = now
    session.flush()


def _register() -> None:
    import jobs
    jobs.register_handler("widget_chat", run_widget_chat_job)


_register()
