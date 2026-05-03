"""Hermes — notifier bot.

The workspace's mouth. Polls its command queue for `hermes.post`
commands, posts the included text to the named notification channel
via Lightsei's existing notifications dispatcher (Phase 9), retries
once on transport failure, then emits either `hermes.posted`
(success) or `hermes.send_failed` (terminal failure).

Hermes does NOT format messages beyond passing the upstream agent's
`text` straight through — Atlas builds its own "✅ atlas: 322 passed"
line before calling `send_command("hermes", "hermes.post", {...})`,
and Hermes hands that text to whichever channel was named. Keeps
the agent ↔ channel coupling thin: each upstream agent decides what
to say, Hermes only decides how it gets there.

Phase 11.4 scope: one inbound kind (`hermes.post`), no downstream
dispatches (Hermes is a leaf), two event types (`hermes.posted` on
success, `hermes.send_failed` on terminal failure). DM-style fan-out
to per-user phone numbers / Telegram chat IDs is parking-lot work.

Env (defaults in parens):
  HERMES_POLL_S            seconds between claim attempts (5)
  HERMES_DEFAULT_CHANNEL   channel name to use when payload omits it
                           ("default")
  HERMES_RETRY_DELAY_S     wait between transport-failure retries (5)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required.

Public surface (for tests, not for production callers):
  classify_outcome(http_status)
      Pure function. Maps the dispatch endpoint's reported HTTP
      status into one of:
        "ok"     — 2xx, treat as delivered.
        "retry"  — 5xx or transport failure (-1 in our convention).
                   Worth retrying once.
        "fail"   — 4xx that isn't worth retrying (auth, bad URL).
                   Surface as hermes.send_failed.

  tick(client, dispatcher, *, default_channel)
      One iteration. Claims a hermes.post command, calls the
      injected dispatcher (mockable in tests), retries once on a
      retry-class outcome, emits hermes.posted or hermes.send_failed,
      completes the command.
"""
import os
import sys
import time
import traceback
from typing import Any, Callable, Optional

import httpx

import lightsei


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("HERMES_POLL_S", "5"))
DEFAULT_CHANNEL = os.environ.get("HERMES_DEFAULT_CHANNEL", "default")
RETRY_DELAY_S = float(os.environ.get("HERMES_RETRY_DELAY_S", "5"))


# ---------- Status classification ---------- #


def classify_outcome(http_status: Optional[int]) -> str:
    """One-liner that reduces an HTTP status into the bot's three
    decision branches:
      ok     — delivered, complete the command.
      retry  — transport failure / 5xx, try once more.
      fail   — 4xx, terminal. Don't retry — auth or bad URL needs
               human action.
    Public for tests; the bot itself uses it inline below.
    """
    if http_status is None:
        return "retry"  # transport failure, treat as retry-class
    if 200 <= http_status < 300:
        return "ok"
    if 500 <= http_status < 600:
        return "retry"
    if http_status < 0:
        # Our notifications module reports -1 / -2 / etc. for in-process
        # errors before the request leaves the box. Same retry posture
        # as a 5xx — could be a transient DNS blip.
        return "retry"
    return "fail"


# ---------- Dispatcher (DI seam for tests) ---------- #


# Returns the dispatch endpoint's response body (already a dict).
Dispatcher = Callable[[str, str, str], dict[str, Any]]


def _dispatch_via_backend(
    channel_name: str, text: str, severity: str
) -> dict[str, Any]:
    """Production dispatcher: POST /workspaces/me/notifications/dispatch
    against the workspace's API. The backend runs the configured
    channel's formatter + posts to the actual webhook + records the
    delivery row.

    Returns the delivery dict. Tests inject a stub instead.
    """
    base = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "LIGHTSEI_API_KEY missing — cannot dispatch to channel"
        )
    r = httpx.post(
        f"{base}/workspaces/me/notifications/dispatch",
        json={
            "channel_name": channel_name,
            "text": text,
            "severity": severity,
        },
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()


# ---------- Bot loop ---------- #


def tick(
    client: Any,
    dispatcher: Dispatcher = _dispatch_via_backend,
    *,
    default_channel: str = "default",
    retry_delay_s: float = 5.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Optional[dict[str, Any]]:
    """One iteration: claim → dispatch (with one retry on retry-class
    outcomes) → emit → complete.

    Returns the claimed command's serialized form when Hermes
    handled something; None when the queue was empty. `sleep` is
    injected so tests can fast-forward retries instead of actually
    waiting 5s.
    """
    cmd = lightsei.claim_command(agent_name="hermes")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "hermes.post":
        lightsei.complete_command(
            cmd_id, error=f"hermes does not handle kind={kind!r}"
        )
        return cmd

    payload = cmd.get("payload") or {}
    channel = payload.get("channel") or default_channel
    text = payload.get("text") or ""
    severity = payload.get("severity") or "info"

    attempts = 0
    last_response: Optional[dict[str, Any]] = None
    last_error: Optional[str] = None
    final_outcome = "fail"  # default if everything goes sideways

    while attempts < 2:
        attempts += 1
        try:
            response = dispatcher(channel, text, severity)
            last_response = response
            delivery = (response or {}).get("delivery") or {}
            http_status = (
                (delivery.get("response_summary") or {}).get("http_status")
            )
            outcome = classify_outcome(http_status)
            final_outcome = outcome
            if outcome == "ok" or outcome == "fail":
                break
            # retry: wait + loop. The first retry is the only retry —
            # transient blips clear in seconds, persistent ones aren't
            # worth burning more budget.
            if attempts < 2:
                sleep(retry_delay_s)
        except Exception as e:
            last_error = repr(e)
            final_outcome = "retry"
            if attempts < 2:
                sleep(retry_delay_s)
            else:
                final_outcome = "fail"

    delivery = (last_response or {}).get("delivery") or {}
    response_summary = delivery.get("response_summary") or {}
    http_status = response_summary.get("http_status")

    if final_outcome == "ok":
        lightsei.emit(
            "hermes.posted",
            {
                "command_id": cmd_id,
                "channel": channel,
                "channel_id": delivery.get("channel_id"),
                "http_status": http_status,
                "attempt_count": attempts,
            },
        )
        lightsei.complete_command(
            cmd_id,
            result={
                "channel": channel,
                "http_status": http_status,
                "attempts": attempts,
            },
        )
    else:
        # fail (4xx) or retry-class that exhausted its second try.
        # Both surface as hermes.send_failed — the user-visible
        # difference is the http_status field, which the dashboard
        # can render differently.
        lightsei.emit(
            "hermes.send_failed",
            {
                "command_id": cmd_id,
                "channel": channel,
                "http_status": http_status,
                "attempt_count": attempts,
                "response_summary": response_summary,
                "error": last_error,
            },
        )
        lightsei.complete_command(
            cmd_id,
            error=(
                last_error
                or f"channel {channel!r} returned {http_status}"
            ),
        )

    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "hermes")
    if not api_key:
        print("hermes: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)

    print(
        f"hermes up: agent={agent_name} default_channel={DEFAULT_CHANNEL}",
        flush=True,
    )

    while True:
        try:
            handled = tick(
                lightsei,
                default_channel=DEFAULT_CHANNEL,
                retry_delay_s=RETRY_DELAY_S,
            )
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"hermes tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
