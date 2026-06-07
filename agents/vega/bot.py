"""Vega — PR reviewer bot.

Phase 13.2. The constellation's reviewer. Polls its command queue for
`vega.review` commands, runs a heuristic pass over the supplied unified
diff, and emits a `vega.review_complete` event with structured review
comments. When the review found something, Vega dispatches a
`hermes.post` summary so the verdict lands wherever Hermes delivers.

Vega is deliberately a heuristic reviewer, not an LLM one: it catches
the boring-but-common stuff (debug statements left in, swallowed
exceptions, eval/exec, skipped tests, a source change with no test
change, an oversized diff). It is cheap, deterministic, and never wrong
about whether a `print(` is still in the patch. Same bot contract as
Atlas + Argus: claim -> pure-function review -> emit -> dispatch -> complete.

Phase 13.2 scope: one command kind (`vega.review`), one downstream
dispatch (`hermes.post`, only when there are comments), two event types
(`vega.review_complete` + `vega.crash`).

Env (defaults in parens):
  VEGA_POLL_S         seconds between claim attempts (5)
  VEGA_HERMES_CHANNEL channel name to pass to Hermes (default)
  VEGA_LARGE_DIFF     added-line count that trips the large-diff note (400)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required; the bot authenticates with this.

Public surface (for tests):
  review_diff(patch, *, large_diff=400) -> [comment]
      Pure function; takes a unified diff and returns review comments.
  tick(client, *, hermes_channel=..., large_diff=...)
      One loop iteration.
  main()  Production entry point.
"""
import os
import re
import sys
import time
import traceback
from typing import Any, Iterator, Optional

import lightsei


def _send_with_source(
    target_agent: str, kind: str, payload: dict[str, Any], *, source_agent: str
) -> dict[str, Any]:
    """send_command with optional source_agent; tolerant of the 0.1.0 SDK."""
    try:
        return lightsei.send_command(
            target_agent, kind, payload, source_agent=source_agent
        )
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("VEGA_POLL_S", "5"))
HERMES_CHANNEL = os.environ.get("VEGA_HERMES_CHANNEL", "default")
LARGE_DIFF = int(os.environ.get("VEGA_LARGE_DIFF", "400"))


# ---------- Diff parsing ---------- #

_TEST_PATH_RE = re.compile(r"(^|/)(test_|.*_test\.|tests?/)|\.(test|spec)\.", re.I)


def _iter_added_lines(patch: str) -> Iterator[tuple[Optional[str], int, str]]:
    """Yield (file, new_line_number, content) for every added (+) line in
    a unified diff. Tracks the current file from `+++ b/...` headers and
    the new-file line counter from `@@ ... +c,d @@` hunk headers."""
    file: Optional[str] = None
    new_line = 0
    for raw in patch.splitlines():
        if raw.startswith("+++ "):
            p = raw[4:].strip()
            if p.startswith("b/"):
                p = p[2:]
            file = None if p == "/dev/null" else p
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_line = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            yield file, new_line, raw[1:]
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            pass  # removed line: no new-file advance
        elif raw.startswith(" "):
            new_line += 1


# (regex, severity, message) run against each added line's content.
_LINE_CHECKS: list[tuple["re.Pattern[str]", str, str]] = [
    (re.compile(r"\beval\s*\(|\bexec\s*\("), "high", "use of eval/exec"),
    (re.compile(r"except\s*:\s*$|except\s+\w+\s*:\s*pass\b"), "high", "bare or swallowed exception"),
    (re.compile(r"@pytest\.mark\.skip|\.skip\(|\bxfail\b|\.only\("), "high", "skipped or focused test"),
    (re.compile(r"\bprint\s*\(|console\.(log|debug)\s*\(|\bdebugger\b|breakpoint\s*\(|pdb\.set_trace"), "medium", "debug statement left in"),
    (re.compile(r"\b(TODO|FIXME|XXX|HACK)\b"), "low", "leftover TODO/FIXME"),
]


def review_diff(patch: str, *, large_diff: int = 400) -> list[dict[str, Any]]:
    """Heuristic review of a unified diff. Public for tests.

    Returns one comment per finding: {severity, file, line, message}.
    File-level comments (no specific line) carry line=None.
    """
    comments: list[dict[str, Any]] = []
    if not patch:
        return comments

    added = list(_iter_added_lines(patch))
    touched_files = {f for f, _, _ in added if f}
    touched_source = {f for f in touched_files if not _TEST_PATH_RE.search(f)}
    touched_tests = {f for f in touched_files if _TEST_PATH_RE.search(f)}

    for file, line, content in added:
        for pattern, severity, message in _LINE_CHECKS:
            if pattern.search(content):
                comments.append(
                    {"severity": severity, "file": file, "line": line, "message": message}
                )

    # File-level advisories.
    if touched_source and not touched_tests:
        comments.append(
            {
                "severity": "low",
                "file": None,
                "line": None,
                "message": "source changed but no test changes in this diff",
            }
        )
    if len(added) > large_diff:
        comments.append(
            {
                "severity": "low",
                "file": None,
                "line": None,
                "message": f"large diff ({len(added)} added lines); consider splitting",
            }
        )
    return comments


def _severity_counts(comments: list[dict[str, Any]]) -> dict[str, int]:
    out = {"high": 0, "medium": 0, "low": 0}
    for c in comments:
        out[c["severity"]] = out.get(c["severity"], 0) + 1
    return out


def hermes_text_for(
    comments: list[dict[str, Any]], added_lines: int, commit: Optional[str]
) -> str:
    """One-line review summary Vega hands to Hermes. Only called when
    there is at least one comment."""
    c = _severity_counts(comments)
    icon = "⚠️" if c["high"] else "\U0001f440"  # warning vs eyes
    suffix = f" at commit {commit[:7]}" if commit else ""
    return (
        f"{icon} vega: reviewed {added_lines} added line"
        f"{'s' if added_lines != 1 else ''} — {len(comments)} comment"
        f"{'s' if len(comments) != 1 else ''} "
        f"({c['high']} high, {c['medium']} medium, {c['low']} low){suffix}"
    )


# ---------- Bot loop ---------- #


def tick(
    client: Any, *, hermes_channel: str = "default", large_diff: int = 400
) -> Optional[dict[str, Any]]:
    """One iteration: claim -> review -> emit -> (maybe) dispatch -> complete."""
    cmd = lightsei.claim_command(agent_name="vega")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "vega.review":
        lightsei.complete_command(
            cmd_id, error=f"vega does not handle kind={kind!r}"
        )
        return cmd

    payload = cmd.get("payload") or {}
    patch = payload.get("diff") or payload.get("patch") or ""
    commit = payload.get("commit")

    try:
        comments = review_diff(patch, large_diff=large_diff)
        added_lines = sum(1 for _ in _iter_added_lines(patch))
    except Exception as e:
        lightsei.emit(
            "vega.crash",
            {"command_id": cmd_id, "error": repr(e), "traceback": traceback.format_exc()},
        )
        try:
            _send_with_source(
                "hermes",
                "hermes.post",
                {
                    "channel": hermes_channel,
                    "text": f"⚠️ vega: crashed reviewing ({type(e).__name__})",
                    "severity": "error",
                },
                source_agent="vega",
            )
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    counts = _severity_counts(comments)
    outcome = {
        "command_id": cmd_id,
        "added_lines": added_lines,
        "comments_count": len(comments),
        "high_severity_count": counts["high"],
        "comments": comments,
        "severity": "error" if counts["high"] else "info",
    }
    if commit:
        outcome["commit"] = commit

    lightsei.emit("vega.review_complete", outcome)

    # A silent reviewer is useless; a noisy one gets muted. Post the
    # verdict to Hermes whenever there's at least one comment.
    if comments:
        try:
            _send_with_source(
                "hermes",
                "hermes.post",
                {
                    "channel": hermes_channel,
                    "text": hermes_text_for(comments, added_lines, commit),
                    "severity": outcome["severity"],
                },
                source_agent="vega",
            )
        except Exception as e:
            print(f"vega: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "vega")
    if not api_key:
        print("vega: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    from lightsei._commands import _handlers as _ls_handlers
    _ls_handlers.clear()

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)
    print(f"vega up: agent={agent_name} channel={HERMES_CHANNEL}", flush=True)

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL, large_diff=LARGE_DIFF)
            if handled is None:
                time.sleep(POLL_S)
        except Exception:
            print(f"vega tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
