"""Atlas — test-runner bot.

The first executor in the Lightsei constellation. Polls its command
queue for `atlas.run_tests` commands, runs `pytest` (or whatever
ATLAS_PYTEST_ARGS is set to) in a subprocess, parses the summary, and
emits an `atlas.tests_run` event with structured outcome. Then
dispatches a `hermes.post` command to Hermes with a tidy summary line
so the result lands wherever Hermes is configured to deliver it.

Atlas does NOT call Slack or any other notification channel directly
— that's Hermes's job. Atlas just produces a structured outcome and
hands the "tell someone" responsibility off via the dispatch chain.

Phase 11.3 scope: one command kind (`atlas.run_tests`), one downstream
dispatch (`hermes.post`), one event type (`atlas.tests_run` plus
`atlas.crash` for the failure path). More command kinds — running a
single test file, re-running a flaky test, opening a PR with a
suggested fix — are Phase 13+ work.

Env (defaults in parens):
  ATLAS_POLL_S         seconds between claim attempts (5)
  ATLAS_PYTEST_ARGS    args to pytest (backend/tests/)
  ATLAS_TEST_DIR       working directory for pytest (the bundle root)
  ATLAS_TIMEOUT_S      per-test-run timeout in seconds (300)
  ATLAS_LOG_TAIL_BYTES bytes of stdout/stderr to attach to events (4096)
  ATLAS_HERMES_CHANNEL channel name to pass to Hermes (default)

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY  required; the bot authenticates with this.

Public surface (for tests, not for production callers):
  build_outcome(stdout, stderr, returncode, duration_s)
      Pure function; takes the subprocess's output and returns the
      shape that gets emitted as the atlas.tests_run event payload.
      Tested in isolation so the regex + summary inference don't
      need a real pytest run.

  tick(client, run_pytest, *, agent_name=...)
      One iteration of the bot's main loop. Claims a command via the
      injected client, runs the injected `run_pytest` callable, emits
      the event, dispatches Hermes. Tests pass mocks for both
      collaborators.

  main()
      Production entry point. Wires the real lightsei client + the
      real subprocess-based pytest runner and loops forever.
"""
import os
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

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
    PyPI 0.1.0 SDK it raises TypeError. The chain_id still propagates
    via the SDK's thread-local context, so dropping the flag yields a
    working chain — just without source-edge attribution in the 11.6
    constellation view. Atlas is robust to either SDK so deploys don't
    have to chase a release.
    """
    try:
        return lightsei.send_command(
            target_agent, kind, payload, source_agent=source_agent
        )
    except TypeError:
        return lightsei.send_command(target_agent, kind, payload)


# ---------- Configuration ---------- #

POLL_S = float(os.environ.get("ATLAS_POLL_S", "5"))
PYTEST_ARGS = os.environ.get("ATLAS_PYTEST_ARGS", "backend/tests/")
TEST_DIR = Path(os.environ.get("ATLAS_TEST_DIR", ".")).resolve()
TIMEOUT_S = float(os.environ.get("ATLAS_TIMEOUT_S", "300"))
LOG_TAIL_BYTES = int(os.environ.get("ATLAS_LOG_TAIL_BYTES", "4096"))
HERMES_CHANNEL = os.environ.get("ATLAS_HERMES_CHANNEL", "default")


# ---------- Pytest output parsing ---------- #

# pytest's terminal summary lands in a few flavors depending on whether
# tests passed, failed, errored, or were collected as zero. The regex
# below handles the common cases without trying to be exhaustive —
# anything we don't recognize falls through to a returncode-based
# inference (returncode=0 → pass, anything else → fail).
_PYTEST_SUMMARY_RE = re.compile(
    r"=+ "
    r"(?:(?P<failed>\d+) failed)?"
    r"(?:,?\s*(?P<passed>\d+) passed)?"
    r"(?:,?\s*(?P<errors>\d+) errors?)?"
    r"(?:,?\s*(?P<skipped>\d+) skipped)?"
    r"(?:,?\s*(?P<warnings>\d+) warnings?)?"
    r"\s+in\s+(?P<duration>\d+(?:\.\d+)?)s",
    re.IGNORECASE,
)


def build_outcome(
    *,
    stdout: str,
    stderr: str,
    returncode: int,
    duration_s: float,
) -> dict[str, Any]:
    """Parse a pytest run's output into a structured outcome.

    Public for tests. Combines stdout + stderr's last LOG_TAIL_BYTES
    into the `log_tail` field, parses the pytest summary line for
    counts, and infers severity from returncode.
    """
    combined = (stdout or "") + (stderr or "")
    # Search backwards from end so we hit the FINAL summary line if
    # pytest emitted multiple.
    summary_line: Optional[str] = None
    for line in reversed(combined.splitlines()):
        if "passed" in line or "failed" in line or "error" in line.lower():
            if "in " in line and line.lstrip().startswith("="):
                summary_line = line.strip()
                break

    passed = failed = errors = skipped = 0
    parsed_duration: Optional[float] = None
    if summary_line:
        m = _PYTEST_SUMMARY_RE.search(summary_line)
        if m:
            passed = int(m.group("passed") or 0)
            failed = int(m.group("failed") or 0)
            errors = int(m.group("errors") or 0)
            skipped = int(m.group("skipped") or 0)
            d = m.group("duration")
            if d:
                parsed_duration = float(d)

    severity = "info" if returncode == 0 else "error"

    tail = combined[-LOG_TAIL_BYTES:] if LOG_TAIL_BYTES > 0 else ""

    return {
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "skipped": skipped,
        "returncode": returncode,
        "duration_s": parsed_duration if parsed_duration is not None else duration_s,
        "summary": summary_line or "",
        "severity": severity,
        "log_tail": tail,
    }


def hermes_text_for(outcome: dict[str, Any], commit: Optional[str]) -> str:
    """One-line message Atlas hands to Hermes for posting. Atlas
    formats the line so Hermes is a thin formatter — keeps Atlas's
    structured outcome decoupled from any particular channel's
    rendering quirks.
    """
    icon = "✅" if outcome["severity"] == "info" else "❌"
    parts = [f"{outcome['passed']} passed"]
    if outcome["failed"]:
        parts.append(f"{outcome['failed']} failed")
    if outcome["errors"]:
        parts.append(f"{outcome['errors']} errors")
    if outcome["skipped"]:
        parts.append(f"{outcome['skipped']} skipped")
    body = ", ".join(parts)
    suffix = f" at commit {commit[:7]}" if commit else ""
    return f"{icon} atlas: {body}{suffix}"


# ---------- Pytest runner (DI seam for tests) ---------- #


PytestRunner = Callable[[str], dict[str, Any]]


def _run_pytest_subprocess(args: str) -> dict[str, Any]:
    """Default `run_pytest` implementation. Spawns pytest in a
    subprocess, captures output, returns the inputs `build_outcome`
    needs. Tests pass a stub instead.
    """
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest"] + args.split(),
            cwd=str(TEST_DIR),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
        return {
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
            "duration_s": time.monotonic() - started,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "stdout": (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""),
            "stderr": (e.stderr or b"").decode("utf-8", errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or ""),
            "returncode": -1,
            "duration_s": time.monotonic() - started,
            "timed_out": True,
        }


# ---------- Bot loop ---------- #


def tick(
    client: Any,
    run_pytest: PytestRunner = _run_pytest_subprocess,
    *,
    hermes_channel: str = "default",
) -> Optional[dict[str, Any]]:
    """One iteration: claim → run → emit → dispatch → complete.

    Returns the claimed command's serialized form when Atlas claimed
    + processed something; None when the queue was empty. Public so
    tests can drive the loop one tick at a time with a mock client +
    mock pytest runner.
    """
    cmd = lightsei.claim_command(agent_name="atlas")
    if cmd is None:
        return None
    cmd_id = cmd.get("id")
    kind = cmd.get("kind") or ""
    if kind != "atlas.run_tests":
        # Atlas handles only one command kind in Phase 11.3. Anything
        # else gets completed-as-failed so it doesn't sit forever.
        lightsei.complete_command(cmd_id, error=f"atlas does not handle kind={kind!r}")
        return cmd

    payload = cmd.get("payload") or {}
    args = payload.get("pytest_args") or PYTEST_ARGS
    commit = payload.get("commit")

    try:
        result = run_pytest(args)
    except Exception as e:
        # Crash path: emit atlas.crash, dispatch a Hermes message that
        # makes the failure visible, complete with failed.
        lightsei.emit(
            "atlas.crash",
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
                    "text": f"⚠️ atlas: crashed running pytest ({type(e).__name__})",
                    "severity": "error",
                },
                source_agent="atlas",
            )
        except Exception:
            # Hermes might be down; the emit above already preserves
            # the crash record so we don't block command completion.
            pass
        lightsei.complete_command(cmd_id, error=repr(e))
        return cmd

    if result.get("timed_out"):
        lightsei.emit(
            "atlas.crash",
            {
                "command_id": cmd_id,
                "error": "pytest timed out",
                "timeout_s": TIMEOUT_S,
            },
        )
        try:
            _send_with_source(
                "hermes",
                "hermes.post",
                {
                    "channel": hermes_channel,
                    "text": f"⏱ atlas: pytest timed out after {int(TIMEOUT_S)}s",
                    "severity": "error",
                },
                source_agent="atlas",
            )
        except Exception:
            pass
        lightsei.complete_command(cmd_id, error="pytest timed out")
        return cmd

    outcome = build_outcome(
        stdout=result["stdout"],
        stderr=result["stderr"],
        returncode=result["returncode"],
        duration_s=result["duration_s"],
    )
    outcome["command_id"] = cmd_id
    if commit:
        outcome["commit"] = commit

    lightsei.emit("atlas.tests_run", outcome)

    # Dispatch to Hermes so the result gets to the human. This send
    # inherits the dispatch_chain_id from the active claim's
    # threading.local context (Phase 11.1).
    try:
        _send_with_source(
            "hermes",
            "hermes.post",
            {
                "channel": hermes_channel,
                "text": hermes_text_for(outcome, commit),
                "severity": outcome["severity"],
            },
            source_agent="atlas",
        )
    except Exception as e:
        # Hermes dispatch failure shouldn't fail the underlying work;
        # the outcome event is already on the wire.
        print(f"atlas: hermes dispatch failed: {e}", flush=True)

    lightsei.complete_command(cmd_id, result=outcome)
    return cmd


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "atlas")
    if not api_key:
        print("atlas: LIGHTSEI_API_KEY missing — refusing to start", flush=True)
        sys.exit(2)

    lightsei.init(api_key=api_key, agent_name=agent_name, base_url=base_url)

    print(
        f"atlas up: agent={agent_name} pytest={PYTEST_ARGS} "
        f"test_dir={TEST_DIR} timeout={int(TIMEOUT_S)}s",
        flush=True,
    )

    while True:
        try:
            handled = tick(lightsei, hermes_channel=HERMES_CHANNEL)
            if handled is None:
                # Queue empty — back off the regular poll interval.
                time.sleep(POLL_S)
            # If we did handle a command, loop immediately and try to
            # claim the next one — keeps us drained on bursts without
            # waiting POLL_S between jobs.
        except Exception:
            print(f"atlas tick crashed:\n{traceback.format_exc()}", flush=True)
            time.sleep(POLL_S)


if __name__ == "__main__":
    main()
