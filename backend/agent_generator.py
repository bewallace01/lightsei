"""Phase 12B.1: backend logic for `POST /workspaces/me/agents/generate`.

Pure module — no FastAPI imports — so the prompt-building + dictionary +
schema bits can be tested in isolation. The endpoint handler in main.py
imports these and adds HTTP / DB / cost-cap / Anthropic-client wiring.

Design choices:
- The agent name returned in `agent_name_suggestion` must come from
  STAR_DICTIONARY. We constrain in two places: the system prompt names
  the dictionary as the only legal source, AND the endpoint validates
  the response and rejects (with one retry) if the LLM picks something
  off-dictionary or already-in-use in the workspace. Belt + suspenders
  because LLMs follow naming conventions inconsistently.
- The submit_bot tool schema mirrors Polaris's `submit_plan` pattern
  (Phase 6.5): forced tool_choice + strict input_schema = guaranteed
  shape, no JSON-parsing retries.
"""
from __future__ import annotations

from typing import Any


# ---------- Star-naming dictionary ---------- #
#
# The seeded bots (polaris, atlas, hermes) set the convention:
# each name is a star / constellation / celestial body whose meaning
# matches the bot's role. Generated bots follow it. Extend over time
# as new roles emerge — the LLM is told "if your role doesn't fit any
# row, ask the user to extend the dictionary," not "invent a name."

STAR_DICTIONARY: list[tuple[str, str]] = [
    ("polaris", "orchestration, navigation, the north star you align by"),
    ("atlas", "bearing weight, running heavy or repeated work (tests, builds, batch jobs)"),
    ("hermes", "messenger, posts notifications outbound (Slack, email, SMS, webhook)"),
    ("argus", "the all-seeing giant — security scanning, secret detection, audit"),
    ("vega", "sharp + bright — code review, PR scrutiny, structural critique"),
    ("sirius", "the dog star, alerting / on-call, the one that pages you"),
    ("cassiopeia", "storyteller in the sky — incident scribe, post-mortem writer"),
    ("lyra", "harmony — coordination, cross-agent integration glue"),
    ("vela", "the sails — deployment, shipping, release verification"),
    ("spica", "wheat-ear, harvest — summarization, digest, weekly recap"),
    ("rigel", "the foot — infrastructure watcher, bedrock health"),
    ("antares", "heart of the scorpion — watching one critical thing closely"),
    ("altair", "flying / fast — realtime streaming, low-latency reactions"),
    ("capella", "the little she-goat herding her kids — fleet monitoring"),
    ("bellatrix", "the warrior — defensive guards, intrusion / abuse detection"),
    ("procyon", "before the dog — pre-commit hooks, pre-flight checks"),
    ("aldebaran", "the follower — cleanup / sweep tasks downstream of others"),
    ("betelgeuse", "red supergiant — long-running batch jobs, overnight work"),
    ("canopus", "second-brightest in the sky — secondary backup, fallback agent"),
    ("arcturus", "the herdsman — managing other bots' lifecycles"),
]


def is_valid_star_name(name: str) -> bool:
    """True iff `name` matches an entry in STAR_DICTIONARY (case-insensitive)."""
    if not isinstance(name, str):
        return False
    n = name.strip().lower()
    return any(star == n for star, _ in STAR_DICTIONARY)


def render_star_dictionary_for_prompt(reserved: set[str]) -> str:
    """Markdown table of available star names for the LLM. Names already
    taken in this workspace are filtered out so the LLM doesn't re-suggest
    them; if reserved is empty (fresh workspace) the full list shows.
    """
    reserved_lower = {n.strip().lower() for n in reserved}
    lines = ["| name | theme / role |", "|---|---|"]
    for star, theme in STAR_DICTIONARY:
        if star in reserved_lower:
            continue
        lines.append(f"| `{star}` | {theme} |")
    return "\n".join(lines)


# ---------- Submit-bot tool schema ---------- #
#
# Forced tool_choice on this schema = guaranteed-shape JSON output.
# Same trick Polaris uses for submit_plan; no JSON-parsing retries.

SUBMIT_BOT_TOOL: dict[str, Any] = {
    "name": "submit_bot",
    "description": (
        "Submit the generated Lightsei bot. Call this tool exactly once "
        "with the full bot.py source, requirements.txt, and a star-themed "
        "agent name picked from the provided dictionary."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": (
                    "A name from the star-naming dictionary in the system "
                    "prompt that thematically matches this bot's role. "
                    "Must not be a name already taken in the workspace."
                ),
            },
            "rationale": {
                "type": "string",
                "description": (
                    "1-3 sentences explaining what the bot does and which "
                    "star-dictionary row matches its role. Written for the "
                    "user to skim, not for code-review consumption."
                ),
            },
            "bot_py": {
                "type": "string",
                "description": (
                    "Full source of bot.py. Must define `main()` as the "
                    "entrypoint, call `lightsei.init(...)` early, and "
                    "register handlers via `@lightsei.on_command(...)` if "
                    "the bot reacts to other agents' dispatches."
                ),
            },
            "requirements_txt": {
                "type": "string",
                "description": (
                    "Full source of requirements.txt. Must include "
                    "`lightsei>=0.1.3` and any other PyPI packages bot.py "
                    "imports outside the standard library."
                ),
            },
        },
        "required": ["agent_name", "rationale", "bot_py", "requirements_txt"],
        "additionalProperties": False,
    },
}


# ---------- System prompt ---------- #


_SDK_REFERENCE = '''
# Lightsei SDK reference

A Lightsei bot is a single Python file (`bot.py`) plus a `requirements.txt`.
The platform's worker installs the deps in a fresh venv and runs `python bot.py`.
The SDK is import-and-init style:

```python
import lightsei
import os

lightsei.init(
    api_key=os.environ["LIGHTSEI_API_KEY"],
    agent_name=os.environ.get("LIGHTSEI_AGENT_NAME", "your-agent-name"),
    version="0.1.0",
)
```

Workspace secrets (set on /account by the user) are injected as env vars at bot
startup — read them with `os.environ` or `lightsei.get_secret(NAME)`.

## Receiving commands from other agents

```python
@lightsei.on_command("argus.scan", description="Scan a commit for secrets.")
def handle_scan(payload: dict) -> dict:
    # payload is the dispatcher's command body. Return value (must be a dict
    # or None) becomes the command's `result` row.
    ...
    return {"findings": [...]}
```

The SDK auto-runs a poller that claims pending commands for this agent and
calls the matching handler. The bot's `main()` typically just blocks forever
in `time.sleep` — handlers run in a daemon thread.

## Dispatching commands to other agents

```python
lightsei.send_command(
    "hermes",                      # target agent name
    "hermes.post",                 # command kind
    {"channel": "default", "text": "scan complete", "severity": "info"},
    source_agent="argus",          # who's dispatching (this bot's name)
)
```

The dispatch chain id propagates automatically via thread-local state set when
the SDK's auto-poller claims a command, so a chain `polaris → argus → hermes`
groups under one chain id in the dashboard's /dispatch view without the bot
having to thread it through.

## LLM calls are auto-instrumented

If the bot uses `openai`, `anthropic`, or `google.generativeai`, just call them
normally — the SDK patches them at init time, captures model + token counts +
content + cost, and emits `llm_call_completed` events automatically. Do NOT
manually wrap LLM calls in `lightsei.emit(...)` — that double-counts.

## Custom events

For non-LLM observability:

```python
lightsei.emit("argus.scan_completed", {
    "command_id": cmd_id,
    "files_scanned": 42,
    "findings": [...],
})
```

## The track decorator

Wrap any function with `@lightsei.track` to make every call to it appear as a
"run" on the dashboard. Useful for the bot's main work loop.

## Coordinate, don't reinvent

If another bot in the workspace already does what you need (a notifier,
a test runner, a deploy verifier), DISPATCH to it via `send_command` rather
than re-implementing the same logic in this bot. The user's existing
constellation is listed below; prefer reaching for it.
'''


_WORKED_EXAMPLES = '''
# Worked examples

## Notifier (hermes-style)

```python
import os
import lightsei

lightsei.init(
    api_key=os.environ["LIGHTSEI_API_KEY"],
    agent_name="hermes",
    version="0.1.0",
)


@lightsei.on_command(
    "hermes.post",
    description="Post a message to a configured notification channel.",
)
def handle_post(payload: dict) -> dict:
    channel = payload.get("channel", "default")
    text = payload.get("text", "")
    severity = payload.get("severity", "info")
    # The platform's /workspaces/me/notifications/dispatch endpoint
    # routes this to whatever channel type the user has wired up
    # (Slack, Discord, Teams, etc.) — we don't care here.
    import httpx
    base = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    r = httpx.post(
        f"{base}/workspaces/me/notifications/dispatch",
        headers={"Authorization": f"Bearer {os.environ['LIGHTSEI_API_KEY']}"},
        json={"channel_name": channel, "text": text, "severity": severity},
        timeout=15.0,
    )
    r.raise_for_status()
    return {"status": "sent", "channel": channel}


def main():
    import time
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
```

## Executor (atlas-style)

```python
import os
import subprocess
import lightsei

lightsei.init(
    api_key=os.environ["LIGHTSEI_API_KEY"],
    agent_name="atlas",
    version="0.1.0",
)


@lightsei.on_command("atlas.run_tests", description="Run pytest and dispatch the result to hermes.")
def handle_run_tests(payload: dict) -> dict:
    cp = subprocess.run(["pytest", "-v"], capture_output=True, text=True, timeout=300)
    passed = "passed" in cp.stdout
    severity = "info" if cp.returncode == 0 else "error"
    icon = "✅" if cp.returncode == 0 else "❌"
    summary = cp.stdout.splitlines()[-1] if cp.stdout else "no output"
    lightsei.send_command(
        "hermes",
        "hermes.post",
        {"channel": "default", "text": f"{icon} atlas: {summary}", "severity": severity},
        source_agent="atlas",
    )
    return {"returncode": cp.returncode, "summary": summary}


def main():
    import time
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
```

## LLM-calling planner (polaris-style)

```python
import os
import time
import anthropic
import lightsei

lightsei.init(
    api_key=os.environ["LIGHTSEI_API_KEY"],
    agent_name="polaris",
    version="0.1.0",
)


@lightsei.track
def tick():
    client = anthropic.Anthropic()  # auto-instrumented by the SDK
    resp = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=1000,
        messages=[{"role": "user", "content": "Summarize today's project state."}],
    )
    text = resp.content[0].text if resp.content else ""
    lightsei.emit("polaris.plan", {"text": text})


def main():
    while True:
        tick()
        time.sleep(3600)


if __name__ == "__main__":
    main()
```
'''


def build_system_prompt(
    *,
    existing_agents: list[dict[str, Any]],
    reserved_names: set[str],
) -> str:
    """Assemble the full system prompt the LLM sees.

    `existing_agents` is the list returned by `/workspaces/me/agents`,
    optionally enriched with each agent's command kinds (the manifest).
    `reserved_names` is the set of agent names already in use in this
    workspace — those rows get filtered out of the star dictionary in
    the prompt so the LLM doesn't propose a collision.
    """
    if existing_agents:
        agents_block = "\n".join(
            (
                f"- `{a['name']}` ({a.get('role') or 'unknown role'})"
                + (
                    f" — pinned to {a['provider']}/{a['model']}"
                    if a.get("provider") and a.get("model")
                    else ""
                )
                + (
                    f" — handles: {', '.join(a['command_kinds'])}"
                    if a.get("command_kinds")
                    else ""
                )
            )
            for a in existing_agents
        )
    else:
        agents_block = "_(no other agents in this workspace yet)_"

    star_table = render_star_dictionary_for_prompt(reserved_names)

    return f"""You generate Lightsei bots from a natural-language description.

Your job: write the source code for one bot that fulfills the user's request,
following the SDK conventions below and dispatching to existing agents in the
user's workspace where it makes sense rather than re-implementing.

{_SDK_REFERENCE}

{_WORKED_EXAMPLES}

# This workspace's existing agents

{agents_block}

When the description's work overlaps with one of these agents, dispatch to
it via `send_command(target, kind, ...)` instead of re-doing the work.

# Star-naming dictionary

The `agent_name` you pick MUST come from this list — names already in use
in this workspace are filtered out. Pick the row whose theme/role best
matches what the bot will do.

{star_table}

If the user's request doesn't fit any row, return the closest match and
include a note in `rationale` that the user may want to rename it.

# Output

Call the `submit_bot` tool exactly once with the four required fields.
The bot you ship MUST:

- Run as `python bot.py` with no arguments.
- Define `main()` as the script entrypoint.
- Call `lightsei.init(api_key=..., agent_name=..., version=...)` before any
  `@lightsei.on_command` work or `send_command`.
- Block forever in `main()` (typically a `while True: time.sleep(60)` loop)
  if it only reacts to commands — the SDK's poller runs handlers in a
  daemon thread.
- Pin every imported PyPI package in `requirements.txt`. `lightsei>=0.1.3`
  is required; add others (anthropic, openai, google-generativeai, httpx,
  requests, etc.) as needed.
- Read secrets from `os.environ` (or `lightsei.get_secret(NAME)`) — the
  worker injects every workspace secret as an env var. Common ones:
  `LIGHTSEI_API_KEY` (always present), `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GOOGLE_API_KEY`, `SLACK_WEBHOOK_URL`. Don't hardcode anything.
"""


def build_user_message(
    description: str,
    target_agents: list[str] | None = None,
    name_hint: str | None = None,
) -> str:
    """Concatenate the user's free-form description + structured hints."""
    parts = [f"Description:\n{description.strip()}"]
    if target_agents:
        parts.append(
            "Coordinate with these agents (dispatch to them rather than reimplementing): "
            + ", ".join(f"`{a}`" for a in target_agents)
        )
    if name_hint:
        parts.append(
            f"Name hint (the user prefers this name if it fits the dictionary): `{name_hint}`"
        )
    return "\n\n".join(parts)
