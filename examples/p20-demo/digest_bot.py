"""Phase 20.10 demo: weekly-digest bot.

The bot a Halo CSM would deploy after dropping `digest-readme.md`
into team-from-README. Wires the three Google connectors (Phase
20.3-20.5) plus the Slack respond surface (Phase 19.5) into one
short bot.

Lives in the `internal` trust zone. Required capabilities, granted
on the dashboard / via the team-from-README plan:

    - connector:gmail
    - connector:google_calendar
    - connector:google_drive
    - slack:respond

Run modes:

    # As a long-lived daemon (the worker keeps it alive between
    # weekly triggers). This is the normal mode.
    python digest_bot.py

    # As a one-off command (handy for local testing). Bypasses
    # the @on_command handler and runs the digest immediately.
    python digest_bot.py --once

Environment:

    LIGHTSEI_API_KEY              # workspace API key (required)
    LIGHTSEI_BASE_URL             # default: https://api.lightsei.com
    LIGHTSEI_AGENT_NAME           # default: 'digest'
    DIGEST_SLACK_CHANNEL          # Slack channel id to post to
                                  # (e.g. 'C012345' or '#weekly-pulse')
    DIGEST_DAYS                   # how far ahead to look on Calendar
                                  # (default 7)
    DIGEST_INBOX_QUERY            # default 'is:unread newer_than:7d'
    DIGEST_DRIVE_QUERY            # default 'modifiedTime > '<week-ago>'
                                  # and trashed = false' (computed
                                  # automatically when not set)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import lightsei
from lightsei.errors import LightseiError


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_env() -> dict[str, Any]:
    return {
        "api_key": os.environ.get("LIGHTSEI_API_KEY"),
        "base_url": os.environ.get(
            "LIGHTSEI_BASE_URL", "https://api.lightsei.com",
        ),
        "agent_name": os.environ.get("LIGHTSEI_AGENT_NAME", "digest"),
        "channel": os.environ.get("DIGEST_SLACK_CHANNEL"),
        "days": int(os.environ.get("DIGEST_DAYS", "7")),
        "inbox_query": os.environ.get(
            "DIGEST_INBOX_QUERY", "is:unread newer_than:7d",
        ),
        "drive_query_override": os.environ.get("DIGEST_DRIVE_QUERY"),
    }


def build_digest(env: dict[str, Any]) -> str:
    """Pull events + unread + recent files; format as Slack text.

    Each fetch is wrapped so one upstream blip doesn't take down
    the whole digest. Missing sections render as a single "couldn't
    fetch" line so the operator can see what failed.
    """
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=env["days"])
    week_ago = now - timedelta(days=env["days"])

    # ---------- Calendar ---------- #
    cal_lines: list[str] = []
    try:
        result = lightsei.calendar.list_events(
            time_min=_iso(now),
            time_max=_iso(end),
            max_results=25,
            single_events=True,
        )
        events = (result or {}).get("events", [])
        if events:
            for ev in events:
                start = (ev.get("start") or {}).get("dateTime") \
                    or (ev.get("start") or {}).get("date") or "?"
                summary = ev.get("summary") or "(no title)"
                cal_lines.append(f"• {start} — {summary}")
        else:
            cal_lines.append("_no events_")
    except LightseiError as e:
        cal_lines.append(f"_couldn't fetch Calendar: {e}_")

    # ---------- Gmail ---------- #
    inbox_lines: list[str] = []
    try:
        result = lightsei.gmail.search_inbox(
            env["inbox_query"], max_results=10,
        )
        messages = (result or {}).get("messages", [])
        if messages:
            for m in messages:
                frm = m.get("from") or "?"
                subj = m.get("subject") or "(no subject)"
                inbox_lines.append(f"• {frm} — {subj}")
        else:
            inbox_lines.append("_inbox clear_")
    except LightseiError as e:
        inbox_lines.append(f"_couldn't fetch Gmail: {e}_")

    # ---------- Drive ---------- #
    drive_lines: list[str] = []
    drive_query = env["drive_query_override"] or (
        f"modifiedTime > '{_iso(week_ago)}' and trashed = false"
    )
    try:
        result = lightsei.drive.list_files(
            query=drive_query,
            page_size=20,
            order_by="modifiedTime desc",
        )
        files = (result or {}).get("files", [])
        if files:
            for f in files:
                drive_lines.append(
                    f"• {f.get('name', '?')} "
                    f"(modified {f.get('modifiedTime', '?')})"
                )
        else:
            drive_lines.append("_nothing modified_")
    except LightseiError as e:
        drive_lines.append(f"_couldn't fetch Drive: {e}_")

    week_label = now.strftime("week of %Y-%m-%d")
    return (
        f"*Weekly pulse* ({week_label})\n\n"
        f"*Next {env['days']} days on the calendar:*\n"
        + "\n".join(cal_lines)
        + "\n\n*Unread email (top 10):*\n"
        + "\n".join(inbox_lines)
        + "\n\n*Recently-modified docs:*\n"
        + "\n".join(drive_lines)
    )


def post_digest(env: dict[str, Any]) -> None:
    """Build the digest + post to Slack. Single call site so the
    @on_command handler and the --once branch share the same path."""
    if not env["channel"]:
        print(
            "DIGEST_SLACK_CHANNEL not set; can't post the digest. "
            "Set the env var to a channel id or name and retry.",
            file=sys.stderr,
        )
        return
    text = build_digest(env)
    lightsei.post_slack(
        channel=env["channel"],
        text=text,
        source_agent=env["agent_name"],
    )
    print(f"posted digest to {env['channel']} ({len(text)} chars)")


# Command kind used by an operator-side scheduler (or a manual
# /agents/{name}/commands POST from the dashboard) to fire the
# digest once. Naming convention matches Phase 11 dispatch: the
# kind is `{agent}.{verb}`.
WEEKLY_DIGEST_KIND = "weekly_digest.run"


@lightsei.on_command(WEEKLY_DIGEST_KIND)
def _on_weekly_digest_command(payload: dict[str, Any]) -> dict[str, Any]:
    """Invoked when an operator (or scheduler) fires the
    `weekly_digest.run` command on this bot. Returns a small dict
    that lands in the command's `result` field for ops visibility."""
    env = _read_env()
    try:
        post_digest(env)
        return {"ok": True}
    except Exception as exc:
        traceback.print_exc()
        return {"ok": False, "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Build + post the digest once, then exit. Useful for "
             "local testing without setting up a scheduler.",
    )
    args = parser.parse_args()

    env = _read_env()
    if not env["api_key"]:
        print("LIGHTSEI_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    lightsei.init(
        api_key=env["api_key"],
        agent_name=env["agent_name"],
        version="0.1.0",
        base_url=env["base_url"],
    )

    if args.once:
        post_digest(env)
        lightsei.flush(timeout=2.0)
        return

    # Daemon mode: the Lightsei worker keeps this process running.
    # The @on_command handler above receives the actual triggers; the
    # main loop is just a heartbeat keep-alive so the worker doesn't
    # consider the bot idle.
    print(f"digest bot up: agent={env['agent_name']}; "
          f"awaiting {WEEKLY_DIGEST_KIND!r} commands")
    while True:
        lightsei.emit("digest_idle_tick", {})
        lightsei.flush(timeout=2.0)
        time.sleep(60)


if __name__ == "__main__":
    main()
