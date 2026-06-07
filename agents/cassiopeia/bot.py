"""Cassiopeia — incident scribe bot.

Phase 13.4. The constellation's note-taker. Polls its command queue for
`cassiopeia.record` commands, appends each event to a running incident
timeline, and emits a `cassiopeia.timeline_entry` event with the
formatted line + the running entry count. On lifecycle milestones
(an incident opening or resolving) it dispatches a `hermes.post` so the
start and the close land in front of a human; the noisy middle of an
incident accrues silently in the event stream.

Same bot contract as Atlas/Argus/Vega/Sirius. Entry formatting is a pure
function; the per-incident timeline is the one piece of state Cassiopeia
carries (in-process, fine for the single-worker v1).

Phase 13.4 scope: one command kind (`cassiopeia.record`), one downstream
dispatch (`hermes.post`, only on lifecycle milestones), two event types
(`cassiopeia.timeline_entry` + `cassiopeia.crash`).

Env (defaults in parens):
  CASSIOPEIA_POLL_S         seconds between claim attempts (5)
  CASSIOPEIA_HERMES_CHANNEL channel name to pass to Hermes (default)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required.

Public surface (for tests):
  format_entry(event) -> str
  compose_summary(incident_id, entries) -> str
  tick(client, *, hermes_channel=...)
  main()
"""
import os
import sys
import time
import traceback
from typing import Any, Optional

import lightsei


def _send_with_source(
    target_agent: str, kind: str, payload: dict[str, Any], *, source_agent: str
) -> dict[str, Any]:
    try:
        return lightsei.send_command(
            target_agent, kind, payload, source_agent=source_agent
        )
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("CASSIOPEIA_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("CASSIOPEIA_HERMES_CHANNEL", "default")

# Statuses that warrant a Hermes ping (start + close of an incident).
_OPEN_STATUSES = {"opened", "declared", "open", "started"}
_CLOSE_STATUSES = {"resolved", "closed", "mitigated"}


# ---------- Formatting (pure) ---------- #


def format_entry(event: dict[str, Any]) -> str:
    """Pure. Render one timeline line from an event.

    `at` (an ISO timestamp the caller supplies) is optional; left out of
    this function so it stays deterministic and testable. Falls back to
    sensible defaults when actor / message are missing.
    """
    actor = str(event.get("actor") or "system").strip()
    message = str(
        event.get("message")
        or event.get("action")
        or event.get("summary")
        or ""
    ).strip()
    at = event.get("at")
    prefix = f"[{at}] " if at else ""
    return f"{prefix}{actor}: {message}".rstrip()


def compose_summary(incident_id: str, entries: list[str]) -> str:
    """Pure. A tidy multi-line incident timeline."""
    header = f"Incident {incident_id} — {len(entries)} entr" + (
        "y" if len(entries) == 1 else "ies"
    )
    body = "\n".join(f"  - {e}" for e in entries)
    return f"{header}\n{body}" if body else header


# ---------- Timeline state (the one piece of state) ---------- #

_TIMELINES: dict[str, list[str]] = {}


def hermes_text_for(incident_id: str, entries: list[str], status: str) -> str:
    """One-line milestone message. Only called on open/close milestones."""
    if status in _CLOSE_STATUSES:
        return (
            f"✅ cassiopeia: incident {incident_id} {status} — "
            f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} logged"
        )
    last = entries[-1] if entries else ""
    return f"\U0001f195 cassiopeia: incident {incident_id} {status} — {last}"


# ---------- Bot loop ---------- #


def tick(
    client: Any, *, hermes_channel: str = "default"
) -> Optional[dict[str, Any]]:
    """One iteration: claim -> append -> emit -> (maybe) dispatch -> complete."""
    cmd = lightsei.claim_command(agent_name="cassiopeia")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "cassiopeia.record":
        lightsei.complete_command(
            cmd_id, error=f"cassiopeia does not handle kind={kind!r}"
        )
        return cmd

    payload = cmd.get("payload") or {}
    incident_id = str(payload.get("incident_id") or "unknown")
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    status = str(event.get("status") or "").strip().lower()

    try:
        entry = format_entry(event)
        timeline = _TIMELINES.setdefault(incident_id, [])
        timeline.append(entry)
    except Exception as e:
        lightsei.emit(
            "cassiopeia.crash",
            {"command_id": cmd_id, "error": repr(e), "traceback": traceback.format_exc()},
        )
        try:
            _send_with_source(
                "hermes", "hermes.post",
                {"channel": hermes_channel,
                 "text": f"⚠️ cassiopeia: crashed recording ({type(e).__name__})",
                 "severity": "error"},
                source_agent="cassiopeia",
            )
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    is_milestone = status in _OPEN_STATUSES or status in _CLOSE_STATUSES
    outcome = {
        "command_id": cmd_id,
        "incident_id": incident_id,
        "entry": entry,
        "entry_count": len(timeline),
        "status": status or None,
        "milestone": is_milestone,
        "timeline": list(timeline),
    }
    lightsei.emit("cassiopeia.timeline_entry", outcome)

    if is_milestone:
        try:
            _send_with_source(
                "hermes", "hermes.post",
                {"channel": hermes_channel,
                 "text": hermes_text_for(incident_id, timeline, status),
                 "severity": "error" if status in _OPEN_STATUSES else "info"},
                source_agent="cassiopeia",
            )
        except Exception as e:
            print(f"cassiopeia: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "cassiopeia")
    if not api_key:
        print("cassiopeia: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"cassiopeia up: agent={agent_name} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"cassiopeia tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
