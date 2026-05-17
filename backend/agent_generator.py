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


def build_iteration_message(
    *,
    previous_bot_py: str,
    previous_requirements_txt: str,
    tweak_request: str,
) -> str:
    """Phase 12B.3: a follow-up turn the endpoint sends when the user
    asks for tweaks rather than a fresh generation.

    The prior generation is included verbatim so Claude can diff against
    it; the tweak is the user's instruction. Keeps the same submit_bot
    tool contract — the response shape is identical to a fresh generation.
    """
    return (
        "Here is the bot you generated previously:\n\n"
        "```python\n"
        f"# bot.py\n{previous_bot_py.strip()}\n"
        "```\n\n"
        "```\n"
        f"# requirements.txt\n{previous_requirements_txt.strip()}\n"
        "```\n\n"
        "The user's tweak request:\n\n"
        f"{tweak_request.strip()}\n\n"
        "Update the bot to satisfy the tweak and call submit_bot again. "
        "Keep the same agent_name unless the tweak meaningfully changes "
        "the bot's role and a different star-dictionary name fits better."
    )


# ---------- Phase 12B.4: validation gate ---------- #

# A small whitelist of stdlib top-level modules. Far from exhaustive; it's
# a fast-check seed so import-validation can rule the obvious cases out
# without a full sys.stdlib_module_names lookup. Misses here just mean a
# stdlib import gets flagged as missing-from-requirements; the LLM-retry
# is harmless if that happens once. Real prod check uses
# `sys.stdlib_module_names` at runtime — see `_is_stdlib_module`.

_KNOWN_STDLIB_PREFIXES = {
    # Common ones we don't want to bother sys.stdlib_module_names for.
    "os", "sys", "time", "json", "re", "io", "subprocess", "datetime",
    "pathlib", "typing", "logging", "asyncio", "threading", "uuid",
    "hashlib", "hmac", "base64", "urllib", "http", "email", "csv",
    "collections", "functools", "itertools", "contextlib", "math",
    "random", "secrets", "tempfile", "shutil", "glob", "argparse",
    "configparser", "dataclasses", "enum", "abc", "warnings", "copy",
    "string", "textwrap", "traceback",
}


def _is_stdlib_module(name: str) -> bool:
    """Best-effort check: is the top-level package `name` part of the
    Python stdlib?

    Uses sys.stdlib_module_names where available (3.10+); falls back to
    the seed whitelist above. Importing into the running interpreter to
    check would be slow + side-effecty, so we don't.
    """
    import sys
    head = name.split(".", 1)[0]
    if head in _KNOWN_STDLIB_PREFIXES:
        return True
    stdlib = getattr(sys, "stdlib_module_names", None)
    if stdlib is not None and head in stdlib:
        return True
    return False


def _extract_imports(source: str) -> list[str]:
    """Walk the AST and pull out every top-level package an `import` or
    `from X import` references. Returns the list of head-of-dotted names
    so requirements.txt can be checked against them.
    """
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module.split(".", 1)[0])
    return sorted(imports)


def _parse_requirements(source: str) -> set[str]:
    """Return the set of top-level package names declared in a
    requirements.txt blob. Handles common pin / extra / comment forms;
    skips -r includes and -e editable installs (rare in our world).
    """
    import re
    names: set[str] = set()
    for raw in (source or "").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-"):
            continue  # -r / -e / --extra-index-url etc.
        # Strip extras + version pins to leave the bare distribution name.
        m = re.match(r"^([A-Za-z0-9_.\-]+)", line)
        if m:
            names.add(m.group(1).lower().replace("_", "-"))
    return names


# Common PyPI distribution names that don't match their import-time module
# name. requirements.txt has the dist name; bot.py imports the module.
_DIST_NAME_OVERRIDES = {
    # imported_module_top_level: pypi_dist_name
    "yaml": "pyyaml",
    "bs4": "beautifulsoup4",
    "PIL": "pillow",
    "cv2": "opencv-python",
    "google": "google-generativeai",  # the closest match for our use case
    "anthropic": "anthropic",
    "openai": "openai",
    "lightsei": "lightsei",
}


def validate_generated_bot(
    bot_py: str,
    requirements_txt: str,
) -> list[str]:
    """Phase 12B.4: run every cheap check we can on the LLM's output and
    return a list of problem strings (empty list = passes). The endpoint
    feeds the list back to Claude as corrective context if anything failed
    and retries once.

    Checks:
    - bot.py compiles (no SyntaxError)
    - bot.py defines a `main` callable (looser than top-level `def main():`
      to allow lambdas / re-exports, but it has to exist by name)
    - every import in bot.py is either stdlib, lightsei, or appears in
      requirements.txt (handles common dist-name mismatches)
    - requirements.txt mentions lightsei (any version)
    """
    problems: list[str] = []

    # 1. Syntax compile.
    try:
        compile(bot_py, "<generated bot.py>", "exec")
    except SyntaxError as exc:
        problems.append(
            f"bot.py has a SyntaxError on line {exc.lineno}: {exc.msg}"
        )
        # Don't bother continuing; the AST walk below would also fail.
        return problems

    # 2. main() exists.
    import ast
    tree = ast.parse(bot_py)
    has_main = any(
        (isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "main")
        or (isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "main" for t in n.targets
        ))
        for n in tree.body
    )
    if not has_main:
        problems.append(
            "bot.py does not define a top-level `main` function. The worker "
            "runs `python bot.py`, so the script needs a main() (or an "
            "if __name__ == '__main__' block calling one)."
        )

    # 3. Imports vs requirements.txt.
    imports = _extract_imports(bot_py)
    declared = _parse_requirements(requirements_txt)
    if "lightsei" not in declared:
        problems.append(
            "requirements.txt must include `lightsei>=0.1.3`."
        )
    missing: list[str] = []
    for mod in imports:
        if _is_stdlib_module(mod):
            continue
        if mod == "lightsei":
            continue
        # Apply dist-name overrides — `import yaml` → `pyyaml` in reqs.
        dist = _DIST_NAME_OVERRIDES.get(mod, mod).lower().replace("_", "-")
        if dist not in declared:
            missing.append(f"`{mod}` (expected in requirements.txt as `{dist}`)")
    if missing:
        problems.append(
            "These imports in bot.py aren't declared in requirements.txt: "
            + ", ".join(missing)
        )

    return problems


def build_validation_retry_message(problems: list[str]) -> str:
    """Render the list of validation problems as a corrective follow-up
    turn so Claude can fix them in a second pass.
    """
    lines = [
        "Your previous submit_bot call produced code with the following "
        "issues. Fix every one of them and call submit_bot again.",
        "",
    ]
    for i, p in enumerate(problems, 1):
        lines.append(f"{i}. {p}")
    lines.append("")
    lines.append(
        "Keep the same agent_name unless one of the issues was the name "
        "itself."
    )
    return "\n".join(lines)


# ---------- Phase 12C.6.3: async job handler ---------- #
#
# The synchronous endpoint logic that used to live in
# `main.generate_agent` (the Anthropic-calling part) moves here so the
# in-process runner in `backend/jobs.py` can call it off the request
# path. The endpoint now does only the synchronous-fast bits
# (auth, key + budget gate, input validation) and enqueues a row.
#
# Why re-read the workspace secret in the handler instead of passing the
# decrypted key through the payload JSON: keys don't belong in the jobs
# table even briefly. The DB lookup is cheap.

def run_agent_generation_job(
    session,
    workspace_id: str,
    payload: dict,
) -> dict:
    """Job-runner handler for `kind='agent_generate'`.

    Mirrors the original `POST /workspaces/me/agents/generate` body
    one-for-one, minus the input validation (the endpoint did that
    before enqueue). Returns the same dict the endpoint used to return
    synchronously; the runner persists it to `generation_jobs.result_payload`.

    Failure modes that used to surface as HTTPException now bubble as
    HTTPException too; the runner catches and writes `error`. The
    dashboard's poll loop surfaces the error text verbatim.
    """
    import uuid
    from datetime import datetime, timezone
    from decimal import Decimal
    from typing import Any, Optional

    import anthropic
    from fastapi import HTTPException
    from sqlalchemy import select

    import secrets_crypto
    from cost import workspace_cost_mtd
    from db import ensure_agent
    from models import Agent, Run, Workspace, WorkspaceSecret
    from pricing import compute_cost_usd

    # Local copy of main._anthropic_error_to_http so this module doesn't
    # import main (which imports us). Mirrors the same status mapping.
    def _anthropic_err_to_http(exc: Exception) -> HTTPException:
        status = getattr(exc, "status_code", None)
        if status == 529:
            return HTTPException(
                status_code=503,
                detail=(
                    "Anthropic is overloaded right now (529). The SDK retried "
                    "and gave up — try again in a few seconds."
                ),
            )
        if status == 429:
            return HTTPException(
                status_code=429,
                detail=(
                    "Anthropic rate-limited this workspace's key. Slow down "
                    "(or check your tier limits) and retry."
                ),
            )
        return HTTPException(status_code=502, detail=f"Anthropic API error: {exc}")

    # 1. Re-read the workspace secret. Endpoint already gated on its
    # presence, but a race (user revoked it between enqueue and run) is
    # possible — fail clean.
    secret_row = session.get(WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY"))
    if secret_row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ANTHROPIC_API_KEY was removed from this workspace between "
                "enqueue and run. Re-add it on /account and retry."
            ),
        )
    workspace = session.get(Workspace, workspace_id)
    if workspace and workspace.budget_usd_monthly is not None:
        cost = workspace_cost_mtd(session, workspace_id)
        used = float(cost.get("mtd_usd") or 0)
        cap = float(workspace.budget_usd_monthly)
        if cap > 0 and used >= cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"workspace MTD spend ${used:.2f} >= budget ${cap:.2f}; "
                    "bump the budget on /account to keep generating."
                ),
            )
    try:
        anthropic_key = secrets_crypto.decrypt(secret_row.encrypted_value)
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="failed to decrypt ANTHROPIC_API_KEY (server config issue)",
        )

    description = payload.get("description") or ""
    target_agents = payload.get("target_agents") or None
    name_hint = payload.get("name_hint") or None
    tweak_request = payload.get("tweak_request") or None
    previous_bot_py = payload.get("previous_bot_py") or None
    previous_requirements_txt = payload.get("previous_requirements_txt") or None

    # 2. Snapshot the workspace's constellation for the prompt.
    agent_rows = session.execute(
        select(Agent).where(Agent.workspace_id == workspace_id).order_by(Agent.name)
    ).scalars().all()
    existing_agents: list[dict[str, Any]] = []
    reserved_names: set[str] = set()
    for a in agent_rows:
        reserved_names.add(a.name.strip().lower())
        cmd_kinds: list[str] = []
        for h in a.command_handlers or []:
            kind = (h or {}).get("kind") if isinstance(h, dict) else None
            if isinstance(kind, str):
                cmd_kinds.append(kind)
        existing_agents.append(
            {
                "name": a.name,
                "role": a.role,
                "provider": a.provider,
                "model": a.model,
                "command_kinds": cmd_kinds,
            }
        )

    system_prompt = build_system_prompt(
        existing_agents=existing_agents,
        reserved_names=reserved_names,
    )
    user_msg = build_user_message(
        description,
        target_agents=target_agents,
        name_hint=name_hint,
    )

    iteration_turn: Optional[str] = None
    if tweak_request and previous_bot_py and previous_requirements_txt:
        iteration_turn = build_iteration_message(
            previous_bot_py=previous_bot_py,
            previous_requirements_txt=previous_requirements_txt,
            tweak_request=tweak_request,
        )

    # Now that the call runs off the request path, the edge-timeout
    # pressure is gone — keep max_retries at 5 so a transient 529
    # doesn't bubble as a job failure.
    client = anthropic.Anthropic(api_key=anthropic_key, max_retries=5)
    model = "claude-opus-4-7"

    token_log: list[tuple[str, int, int]] = []

    def _ask(extra_user_msg: Optional[str] = None) -> Any:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
        if iteration_turn:
            messages.append({"role": "user", "content": iteration_turn})
        if extra_user_msg:
            messages.append({"role": "user", "content": extra_user_msg})
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=system_prompt,
            tools=[SUBMIT_BOT_TOOL],
            tool_choice={"type": "tool", "name": "submit_bot"},
            messages=messages,
        )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            token_log.append(
                (
                    getattr(resp, "model", None) or model,
                    int(getattr(usage, "input_tokens", 0) or 0),
                    int(getattr(usage, "output_tokens", 0) or 0),
                )
            )
        return resp

    def _extract(resp: Any) -> dict[str, Any]:
        block = next(
            (
                b for b in resp.content
                if getattr(b, "type", None) == "tool_use"
                and getattr(b, "name", None) == "submit_bot"
            ),
            None,
        )
        if block is None:
            raise HTTPException(
                status_code=502,
                detail=(
                    "model did not call submit_bot "
                    f"(stop_reason={getattr(resp, 'stop_reason', '?')})"
                ),
            )
        return block.input

    try:
        try:
            resp = _ask()
            out = _extract(resp)
        except anthropic.APIError as e:
            raise _anthropic_err_to_http(e)

        suggested = (out.get("agent_name") or "").strip().lower()
        if not is_valid_star_name(suggested) or suggested in reserved_names:
            retry_msg = (
                f"The name `{suggested}` you proposed is "
                + (
                    "already taken in this workspace"
                    if suggested in reserved_names
                    else "not in the star-naming dictionary"
                )
                + ". Pick a different name from the dictionary in the system prompt "
                "and call submit_bot again with the same bot_py/requirements_txt."
            )
            try:
                resp = _ask(extra_user_msg=retry_msg)
                out = _extract(resp)
                suggested = (out.get("agent_name") or "").strip().lower()
            except anthropic.APIError as e:
                raise _anthropic_err_to_http(e)

            if not is_valid_star_name(suggested) or suggested in reserved_names:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"generator could not pick a valid star name "
                        f"(got {suggested!r}); try editing the description or pass "
                        "a name_hint."
                    ),
                )

        bot_py = out.get("bot_py") or ""
        requirements_txt = out.get("requirements_txt") or ""
        problems = validate_generated_bot(bot_py, requirements_txt)
        if problems:
            retry_msg = build_validation_retry_message(problems)
            try:
                resp = _ask(extra_user_msg=retry_msg)
                out = _extract(resp)
            except anthropic.APIError as e:
                raise _anthropic_err_to_http(e)
            suggested = (out.get("agent_name") or "").strip().lower()
            if not is_valid_star_name(suggested) or suggested in reserved_names:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"validation retry produced an invalid name "
                        f"(got {suggested!r})"
                    ),
                )
            bot_py = out.get("bot_py") or ""
            requirements_txt = out.get("requirements_txt") or ""
            remaining = validate_generated_bot(bot_py, requirements_txt)
            if remaining:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "generator could not produce a valid bot after retry; "
                        "remaining issues: " + " | ".join(remaining)
                    ),
                )

        return {
            "agent_name_suggestion": suggested,
            "rationale": out.get("rationale") or "",
            "bot_py": bot_py,
            "requirements_txt": requirements_txt,
            "model_used": getattr(resp, "model", model),
            "tokens_in": getattr(getattr(resp, "usage", None), "input_tokens", None),
            "tokens_out": getattr(getattr(resp, "usage", None), "output_tokens", None),
        }
    finally:
        # Record cost even on failure paths: Anthropic already billed
        # for whatever tokens loaded before the exception. Commit
        # explicitly so the cost survives the runner's session-rollback
        # on handler failure.
        if token_log:
            now_ts = datetime.now(timezone.utc)
            ensure_agent(session, workspace_id, "lightsei.system", now_ts)
            total_in = sum(t[1] for t in token_log)
            total_out = sum(t[2] for t in token_log)
            cost_model = token_log[0][0]
            delta = compute_cost_usd(cost_model, total_in, total_out)
            run_row = Run(
                id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                agent_name="lightsei.system",
                started_at=now_ts,
                ended_at=now_ts,
                cost_usd=Decimal(format(delta, ".6f")),
            )
            session.add(run_row)
            session.commit()


# Register on import so backend/jobs.py's `_load_default_handlers()`
# picks it up at startup.
def _register() -> None:
    import jobs
    jobs.register_handler("agent_generate", run_agent_generation_job)


_register()
