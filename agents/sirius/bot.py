"""Sirius — alert triager / on-call bot.

Phase 13.3. The constellation's on-call. Polls its command queue for
`sirius.triage` commands, classifies each incoming alert's severity,
suppresses duplicates seen inside a recent window (so an alert storm
doesn't page a human a hundred times), and decides an action: page,
notify, or log. It dispatches `hermes.post` for page/notify and stays
silent for log/suppress. Emits a `sirius.triaged` event for every alert.

Same bot contract as Atlas/Argus/Vega. Severity classification is a pure
function (explicit `severity`/`level` field wins; otherwise inferred
from the text). Dedup is the one piece of state Sirius carries: an
in-process fingerprint window, which is fine for the single-worker v1
(a multi-instance on-call would move this to the backend).

Phase 13.3 scope: one command kind (`sirius.triage`), one downstream
dispatch (`hermes.post`), two event types (`sirius.triaged` +
`sirius.crash`).

Env (defaults in parens):
  SIRIUS_POLL_S            seconds between claim attempts (5)
  SIRIUS_HERMES_CHANNEL    channel name to pass to Hermes (default)
  SIRIUS_DEDUP_WINDOW_S    suppress repeats of a fingerprint within (300)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required.

Public surface (for tests):
  classify_severity(alert) -> "high"|"medium"|"low"
  fingerprint_for(alert) -> str
  triage_alert(alert) -> {severity, action, fingerprint, reason}
  tick(client, *, hermes_channel=..., dedup_window_s=...)
  main()
"""
import hashlib
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

POLL_S = float(os.environ.get("SIRIUS_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("SIRIUS_HERMES_CHANNEL", "default")
DEDUP_WINDOW_S = float(os.environ.get("SIRIUS_DEDUP_WINDOW_S", "300"))


# ---------- Severity classification ---------- #

_EXPLICIT = {
    "high": "high", "critical": "high", "fatal": "high", "error": "high",
    "emergency": "high", "sev1": "high", "p1": "high", "page": "high",
    "medium": "medium", "warning": "medium", "warn": "medium",
    "degraded": "medium", "sev2": "medium", "p2": "medium",
    "low": "low", "info": "low", "notice": "low", "debug": "low",
    "sev3": "low", "p3": "low", "resolved": "low",
}
_HIGH_KW = ("critical", "fatal", "emergency", "outage", " down", "unreachable",
            "paging", "sev1", " p1", "503", "5xx", "data loss", "breach")
_MEDIUM_KW = ("warn", "degraded", "elevated", "latency", "sev2", " p2",
              "retry", "slow", "throttl")
_LOW_KW = ("info", "notice", "debug", "sev3", " p3", "resolved", "recovered")


def _alert_text(alert: dict[str, Any]) -> str:
    parts = [str(alert.get(k, "")) for k in ("title", "message", "summary", "body")]
    return " ".join(parts).lower()


def classify_severity(alert: dict[str, Any]) -> str:
    """Pure. Explicit severity/level field wins; otherwise infer from text.
    Defaults to medium when nothing matches (better to over-notify than drop)."""
    explicit = str(alert.get("severity") or alert.get("level") or "").strip().lower()
    if explicit in _EXPLICIT:
        return _EXPLICIT[explicit]
    text = _alert_text(alert)
    if any(kw in text for kw in _HIGH_KW):
        return "high"
    if any(kw in text for kw in _LOW_KW):
        return "low"
    if any(kw in text for kw in _MEDIUM_KW):
        return "medium"
    return "medium"


def fingerprint_for(alert: dict[str, Any]) -> str:
    """Pure. An explicit fingerprint/dedup_key wins; otherwise hash the
    (source, title) so the same recurring alert collapses."""
    explicit = alert.get("fingerprint") or alert.get("dedup_key")
    if explicit:
        return str(explicit)
    basis = f"{alert.get('source', '')}|{alert.get('title', '')}".strip("|")
    return "fp_" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


_ACTION = {"high": "page", "medium": "notify", "low": "log"}


def triage_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """Pure classification (no dedup): severity + action + fingerprint."""
    severity = classify_severity(alert)
    return {
        "severity": severity,
        "action": _ACTION[severity],
        "fingerprint": fingerprint_for(alert),
        "reason": f"classified {severity}",
    }


# ---------- Dedup window (the one piece of state) ---------- #

_SEEN: dict[str, float] = {}


def _is_duplicate(fingerprint: str, window_s: float, now: float) -> bool:
    """Record `fingerprint` at `now`; return True if it was last seen
    within `window_s`. Prunes expired entries as it goes."""
    for fp in [k for k, t in _SEEN.items() if now - t > window_s]:
        del _SEEN[fp]
    last = _SEEN.get(fingerprint)
    _SEEN[fingerprint] = now
    return last is not None and (now - last) <= window_s


def hermes_text_for(triage: dict[str, Any], alert: dict[str, Any]) -> str:
    """One-line on-call message. Only called for page/notify actions."""
    title = str(alert.get("title") or alert.get("message") or "alert").strip()
    if triage["action"] == "page":
        return f"\U0001f534 sirius: PAGE — {title} ({triage['severity']})"
    return f"\U0001f7e1 sirius: notify — {title} ({triage['severity']})"


# ---------- Bot loop ---------- #


def tick(
    client: Any, *, hermes_channel: str = "default", dedup_window_s: float = 300.0
) -> Optional[dict[str, Any]]:
    """One iteration: claim -> triage -> dedup -> emit -> (maybe) dispatch -> complete."""
    cmd = lightsei.claim_command(agent_name="sirius")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "sirius.triage":
        lightsei.complete_command(
            cmd_id, error=f"sirius does not handle kind={kind!r}"
        )
        return cmd

    payload = cmd.get("payload") or {}
    alert = payload.get("alert") if isinstance(payload.get("alert"), dict) else payload

    try:
        triage = triage_alert(alert)
        duplicate = _is_duplicate(triage["fingerprint"], dedup_window_s, time.monotonic())
    except Exception as e:
        lightsei.emit(
            "sirius.crash",
            {"command_id": cmd_id, "error": repr(e), "traceback": traceback.format_exc()},
        )
        try:
            _send_with_source(
                "hermes", "hermes.post",
                {"channel": hermes_channel,
                 "text": f"⚠️ sirius: crashed triaging ({type(e).__name__})",
                 "severity": "error"},
                source_agent="sirius",
            )
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    action = "suppress" if duplicate else triage["action"]
    outcome = {
        "command_id": cmd_id,
        "severity": triage["severity"],
        "action": action,
        "fingerprint": triage["fingerprint"],
        "duplicate": duplicate,
        "reason": "suppressed: duplicate within window" if duplicate else triage["reason"],
    }
    lightsei.emit("sirius.triaged", outcome)

    if action in ("page", "notify"):
        try:
            _send_with_source(
                "hermes", "hermes.post",
                {"channel": hermes_channel,
                 "text": hermes_text_for(triage, alert),
                 "severity": "error" if action == "page" else "info"},
                source_agent="sirius",
            )
        except Exception as e:
            print(f"sirius: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "sirius")
    if not api_key:
        print("sirius: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(
        f"sirius up: agent={agent_name} channel={HERMES_CHANNEL} "
        f"dedup={int(DEDUP_WINDOW_S)}s",
        flush=True,
    )

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL, dedup_window_s=DEDUP_WINDOW_S)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"sirius tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
