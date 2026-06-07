"""Argus — security + secret scanner bot.

Phase 13.1. The constellation's watchman. Polls its command queue for
`argus.scan` commands, scans the supplied text (a blob, or a list of
files) for hardcoded secrets using a curated pattern set, and emits an
`argus.scan_complete` event with the (redacted) findings. If any
high-severity secret is found, Argus dispatches a `hermes.post` security
alert so a human hears about it wherever Hermes is configured to deliver.

Argus never posts to a channel directly, and it never stores a raw
secret: every finding's matched value is masked before it leaves this
process (a secret scanner that logged the secrets it found would be the
leak). Argus produces a structured outcome and hands the "tell someone"
job to Hermes via the dispatch chain, exactly like Atlas does.

Phase 13.1 scope: one command kind (`argus.scan`), one downstream
dispatch (`hermes.post`, only when there is a high-severity finding),
two event types (`argus.scan_complete` on every scan + `argus.crash` on
the failure path). Scanning a whole git diff, ignoring allow-listed
test fixtures, and opening an issue with a redaction patch are later work.

Env (defaults in parens):
  ARGUS_POLL_S         seconds between claim attempts (5)
  ARGUS_HERMES_CHANNEL channel name to pass to Hermes (default)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required; the bot authenticates with this.

Public surface (for tests, not for production callers):
  scan_for_secrets(text, *, path=None)
      Pure function; takes a blob of text and returns the list of
      findings (each masked). Tested in isolation so the pattern set
      doesn't need a real scan target.

  tick(client, *, hermes_channel=...)
      One iteration of the bot's main loop. Claims a command, runs the
      scanner, emits the event, conditionally dispatches Hermes. Tests
      pass a mock client.

  main()
      Production entry point. Wires the real lightsei client + loops.
"""
import math
import os
import re
import sys
import time
import traceback
from collections import Counter
from typing import Any, Optional

import lightsei


def _send_with_source(
    target_agent: str,
    kind: str,
    payload: dict[str, Any],
    *,
    source_agent: str,
) -> dict[str, Any]:
    """`lightsei.send_command(..., source_agent=...)` against any SDK.

    The `source_agent` kwarg landed in Phase 11.5; on the published
    PyPI 0.1.0 SDK it raises TypeError. Drop the flag in that case so a
    stale SDK still yields a working chain (just without source-edge
    attribution). Mirrors Atlas's helper of the same name.
    """
    try:
        return lightsei.send_command(
            target_agent, kind, payload, source_agent=source_agent
        )
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("ARGUS_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("ARGUS_HERMES_CHANNEL", "default")


# ---------- Secret patterns ---------- #

# Each entry: (type, severity, compiled regex). The regex's first
# capturing group, when present, is the secret value to mask; otherwise
# the whole match is masked. Ordered most-specific first so a provider
# token is reported as that provider rather than a generic assignment.
_PATTERNS: list[tuple[str, str, "re.Pattern[str]"]] = [
    ("aws_access_key_id", "high", re.compile(r"\b(AKIA[0-9A-Z]{16})\b")),
    (
        "aws_secret_access_key",
        "high",
        re.compile(
            r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})"
        ),
    ),
    (
        "private_key_block",
        "high",
        re.compile(
            r"(-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----)"
        ),
    ),
    ("github_token", "high", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,})\b")),
    ("slack_token", "high", re.compile(r"\b(xox[baprs]-[A-Za-z0-9-]{10,})\b")),
    (
        "stripe_secret_key",
        "high",
        re.compile(r"\b(sk_(?:live|test)_[A-Za-z0-9]{16,})\b"),
    ),
    ("anthropic_api_key", "high", re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{20,})\b")),
    # Generic provider-style key (e.g. OpenAI) without the anthropic infix.
    ("provider_api_key", "high", re.compile(r"\b(sk-(?!ant-)[A-Za-z0-9]{32,})\b")),
    # Generic "secret = '...'" assignment. Lower confidence, so medium.
    (
        "generic_secret_assignment",
        "medium",
        re.compile(
            r"(?i)(?:api[_-]?key|secret|token|passwd|password|access[_-]?key)"
            r"\s*[=:]\s*['\"]([^'\"]{12,})['\"]"
        ),
    ),
]

# A captured generic-assignment value below this Shannon entropy is
# almost certainly a placeholder ("changeme", "your-token-here") rather
# than a real secret, so we drop it to keep the signal clean.
_MIN_GENERIC_ENTROPY = 3.0


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _mask(secret: str) -> str:
    """Mask a matched secret so the finding never carries the raw value.

    Keep the first 4 and last 2 characters when there's enough length
    to make the rest non-recoverable; otherwise fully redact. Always
    report the length so a reviewer can tell a 40-char key from a typo.
    """
    n = len(secret)
    if n <= 8:
        return f"{'*' * n} ({n} chars)"
    return f"{secret[:4]}{'*' * (n - 6)}{secret[-2:]} ({n} chars)"


def scan_for_secrets(
    text: str, *, path: Optional[str] = None
) -> list[dict[str, Any]]:
    """Scan a blob of text for hardcoded secrets.

    Public for tests. Returns one finding per match:
      {type, severity, line, masked, path}
    The matched value is masked before it is returned; the raw secret
    never leaves this function.
    """
    findings: list[dict[str, Any]] = []
    if not text:
        return findings
    for line_no, line in enumerate(text.splitlines(), start=1):
        for kind, severity, pattern in _PATTERNS:
            for m in pattern.finditer(line):
                value = m.group(1) if m.groups() else m.group(0)
                if (
                    kind == "generic_secret_assignment"
                    and _shannon_entropy(value) < _MIN_GENERIC_ENTROPY
                ):
                    continue
                findings.append(
                    {
                        "type": kind,
                        "severity": severity,
                        "line": line_no,
                        "masked": _mask(value),
                        "path": path,
                    }
                )
    return findings


def _collect_targets(payload: dict[str, Any]) -> list[tuple[Optional[str], str]]:
    """Normalize the command payload into (path, text) pairs.

    Accepts either `text` (a single blob, path=None) or
    `files: [{path, content}]`. Unknown shapes yield no targets, which
    the tick reports as a clean zero-finding scan.
    """
    targets: list[tuple[Optional[str], str]] = []
    if isinstance(payload.get("text"), str):
        targets.append((payload.get("path"), payload["text"]))
    files = payload.get("files")
    if isinstance(files, list):
        for f in files:
            if isinstance(f, dict) and isinstance(f.get("content"), str):
                targets.append((f.get("path"), f["content"]))
    return targets


def hermes_text_for(findings: list[dict[str, Any]], commit: Optional[str]) -> str:
    """One-line security alert Argus hands to Hermes. Only called when
    there is at least one high-severity finding."""
    highs = [f for f in findings if f["severity"] == "high"]
    n = len(highs)
    first = highs[0]
    where = f"{first['path']}:{first['line']}" if first.get("path") else f"line {first['line']}"
    suffix = f" at commit {commit[:7]}" if commit else ""
    extra = f" (+{n - 1} more)" if n > 1 else ""
    return (
        f"\U0001f6a8 argus: {n} hardcoded secret"
        f"{'s' if n != 1 else ''} found — {first['type']} at {where}{extra}{suffix}"
    )


# ---------- Bot loop ---------- #


def tick(
    client: Any,
    *,
    hermes_channel: str = "default",
) -> Optional[dict[str, Any]]:
    """One iteration: claim -> scan -> emit -> (maybe) dispatch -> complete.

    Returns the claimed command's serialized form when Argus processed
    something; None when the queue was empty. Public so tests can drive
    the loop one tick at a time with a mock client.
    """
    cmd = lightsei.claim_command(agent_name="argus")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "argus.scan":
        lightsei.complete_command(
            cmd_id, error=f"argus does not handle kind={kind!r}"
        )
        return cmd

    payload = cmd.get("payload") or {}
    commit = payload.get("commit")

    try:
        targets = _collect_targets(payload)
        findings: list[dict[str, Any]] = []
        for path, text in targets:
            findings.extend(scan_for_secrets(text, path=path))
    except Exception as e:
        lightsei.emit(
            "argus.crash",
            {
                "command_id": cmd_id,
                "error": repr(e),
                "traceback": traceback.format_exc(),
            },
        )
        try:
            _send_with_source(
                "hermes",
                "hermes.post",
                {
                    "channel": hermes_channel,
                    "text": f"⚠️ argus: crashed scanning ({type(e).__name__})",
                    "severity": "error",
                },
                source_agent="argus",
            )
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    high_count = sum(1 for f in findings if f["severity"] == "high")
    outcome = {
        "command_id": cmd_id,
        "files_scanned": len(targets),
        "findings_count": len(findings),
        "high_severity_count": high_count,
        "findings": findings,
        "severity": "error" if high_count else "info",
    }
    if commit:
        outcome["commit"] = commit

    lightsei.emit("argus.scan_complete", outcome)

    # Only wake a human for high-severity secrets; medium findings live
    # in the event for review without paging anyone.
    if high_count:
        try:
            _send_with_source(
                "hermes",
                "hermes.post",
                {
                    "channel": hermes_channel,
                    "text": hermes_text_for(findings, commit),
                    "severity": "error",
                },
                source_agent="argus",
            )
        except Exception as e:
            print(f"argus: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "argus")
    if not api_key:
        print("argus: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    # Same reason as Atlas: clear the SDK's auto-registered default
    # handler so the auto-poller never starts and races our tick().
    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)

    print(f"argus up: agent={agent_name} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"argus tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
