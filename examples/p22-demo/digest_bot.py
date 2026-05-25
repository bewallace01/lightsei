"""Phase 22.10 demo: morning briefing bot.

What an internal team would deploy after dropping
`daily-digest-readme.md` into team-from-readme. Demonstrates the
Phase 22 trigger surface end-to-end:

  - `@lightsei.on_trigger` registers the entry point fired by
    cron triggers + webhook triggers (the same handler runs for
    both; the bot branches on `lightsei.trigger.kind`).
  - `lightsei.trigger.scheduled_at` carries the cron-fired
    timestamp (None for webhook fires).
  - `lightsei.trigger.webhook_payload` carries the curl body for
    webhook fires (None for cron fires).
  - `lightsei.trigger.name` is the operator-chosen label, useful
    for log lines + branching logic when multiple triggers point
    at the same bot.

Lives in the `pii` trust zone (the bot would read Gmail +
Calendar; both connectors are pii-only). Capabilities required
(set on the agent in the dashboard before the trigger will do
anything useful):

  - connector:gmail
  - connector:google_calendar
  - slack:respond

Run mode:

    python digest_bot.py

Environment:

    LIGHTSEI_API_KEY      # workspace API key (required)
    LIGHTSEI_BASE_URL     # default: https://api.lightsei.com
    LIGHTSEI_AGENT_NAME   # default: 'morning-briefing'

The handler in this file emits a fake digest (it does NOT call
Gmail / Calendar / Slack so the demo runs without those
connectors set up). A real bot would replace `_build_digest` with
calls to `lightsei.gmail.list_messages(...)` +
`lightsei.calendar.list_events(...)` and post the result via
`lightsei.post_slack(channel="#morning-brief", text=digest)`.

The wedge: even though this bot fires on its own with no human in
the loop, it still runs through the same capability + zone gates
as any other Lightsei bot. A scheduled trigger doesn't bypass
anything.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone

import lightsei


def _build_digest(scheduled_at: datetime | None) -> str:
    """Pretend to assemble the morning briefing.

    Replace this with real connector calls in production:

        events = lightsei.calendar.list_events(
            calendar_id="primary",
            time_min=start_of_day.isoformat(),
            time_max=end_of_day.isoformat(),
        )
        important = lightsei.gmail.list_messages(
            query='label:important is:unread newer_than:1d',
            max_results=10,
        )
        ... assemble + format ...
    """
    when = scheduled_at.strftime("%A %b %d") if scheduled_at else "today"
    return (
        f"# Morning briefing for {when}\n\n"
        "## Calendar\n"
        "- 10:00 - 10:30 - 1:1 with manager\n"
        "- 11:00 - 12:00 - Sprint planning\n"
        "- 14:00 - 15:00 - Customer call (Halo Industries)\n\n"
        "## Email (unread, important)\n"
        "- Q3 budget review (3 messages in thread)\n"
        "- Security audit follow-up (legal)\n"
        "- Halo contract redline\n\n"
        "## Heads up\n"
        "- 14:00 customer call needs the Q3 forecast slides\n"
    )


@lightsei.on_trigger
def handle():
    """Single entry point. Same handler runs for both cron and
    webhook triggers; the bot branches on the trigger kind."""
    kind = lightsei.trigger.kind  # 'cron' / 'webhook' / 'manual'
    name = lightsei.trigger.name or "(unnamed)"
    scheduled_at = lightsei.trigger.scheduled_at  # set on cron fires
    payload = lightsei.trigger.webhook_payload  # set on webhook fires

    print(f"[digest_bot] fired by trigger={name!r} kind={kind}")
    if kind == "cron":
        print(f"[digest_bot]   scheduled_at={scheduled_at!s}")
        reason = "scheduled morning briefing"
    elif kind == "webhook":
        reason = (payload or {}).get("reason", "manual webhook fire")
        print(f"[digest_bot]   webhook payload reason={reason!r}")
    else:
        # `manual` shouldn't happen here — this handler is only
        # entered via the trigger.fire bridge — but defensive.
        reason = "manual"

    lightsei.emit(
        "digest.started",
        {"trigger_kind": kind, "trigger_name": name, "reason": reason},
    )

    try:
        digest = _build_digest(scheduled_at)
    except Exception as exc:
        # Don't let a single broken connector call kill the run
        # silently. Emit a failure event the operator sees in /runs.
        traceback.print_exc()
        lightsei.emit("digest.failed", {"error": repr(exc)})
        return {"ok": False, "error": repr(exc)}

    print("[digest_bot] digest body:\n" + digest)
    lightsei.emit("digest.posted", {"length": len(digest)})

    # In production: lightsei.post_slack(channel="#morning-brief", text=digest)
    # The post_slack call is capability-gated (`slack:respond`); the
    # backend refuses it unless the bot has the capability + the
    # workspace has a connected Slack workspace.

    return {"ok": True, "digest_length": len(digest), "trigger_kind": kind}


def main() -> int:
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "morning-briefing")
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    if not api_key:
        print("error: LIGHTSEI_API_KEY is not set", file=sys.stderr)
        return 1

    lightsei.init(
        api_key=api_key,
        agent_name=agent_name,
        version="0.1.0",
        base_url=os.environ.get("LIGHTSEI_BASE_URL"),
    )

    print(
        f"[digest_bot] registered as {agent_name!r}; waiting for trigger fires."
    )
    # Long-lived loop: the SDK's command poller picks up trigger.fire
    # commands in the background and invokes the @on_trigger handler.
    # Sleep forever so the process stays alive.
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("[digest_bot] shutting down")
        return 0


if __name__ == "__main__":
    sys.exit(main())
