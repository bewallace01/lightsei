"""Polaris — project orchestrator bot.

Reads a project's MEMORY.md and TASKS.md (or a configurable list of
docs) on every tick, calls Claude via a forced `submit_plan` tool call
(strict schema guarantees the structured output), and emits the result
as a `polaris.plan` event so the dashboard can render it.

Phase 10.6 demo marker: this comment was pushed via git on 2026-05-01
to verify the GitHub webhook → push-triggered redeploy loop. The
agent-path mapping for `polaris` covers `polaris/`, so a touch to any
file in that directory should fire the webhook and queue a new
deployment with source=github_push without anyone running the CLI.

Phase 6A scope: read-only. No PRs, no command dispatch. Polaris produces
visible recommendations only. See TASKS.md "Phase 6" for the demo
criterion and the 6B+ roadmap.

Phase 6.5 switched the structured-output mechanism from "ask for JSON
in the prompt and parse" to Anthropic tool use with `strict: true` and
`tool_choice` forced to `submit_plan`. The model now returns a typed
input dict directly — no JSON parser, no parse-error retry path. Also
opted into adaptive thinking with effort=high since orchestrator
planning is intelligence-sensitive (skill guidance for 4.7).

Phase 6.2 added in-process change detection: the bot remembers the
last successfully-emitted doc hashes and skips the LLM call when both
files are byte-identical. A fresh deploy resets that state, so
re-deploying always regenerates a plan even on unchanged docs.

Phase 10.4 adds an optional GitHub fetch path: when POLARIS_GITHUB_REPO
+ POLARIS_GITHUB_TOKEN are set, the bot pulls docs from the repo on
every tick instead of reading bundled disk copies. Combined with the
hash-skip cache, the user can iterate on docs by pushing to GitHub —
no redeploy required, the cache busts on every push that changes a
hashed doc and skips on every push that doesn't. If any of the GitHub
vars are missing, the bot transparently falls back to the disk path.

Env (defaults in parens):
  POLARIS_POLL_S            seconds between ticks (3600)
  POLARIS_MODEL             Claude model id (claude-opus-4-7)
  POLARIS_DOCS_DIR          where to find docs on disk (.)
  POLARIS_DRY_RUN           skip the Anthropic call (unset)
  POLARIS_GITHUB_REPO       owner/name. When set + TOKEN, fetch docs
                            from GitHub instead of disk.
  POLARIS_GITHUB_BRANCH     branch / tag / commit ref (main)
  POLARIS_GITHUB_TOKEN      GitHub PAT with Contents:Read on the repo
  POLARIS_GITHUB_DOCS_PATHS comma-separated repo-relative paths to
                            include (MEMORY.md,TASKS.md). Filenames
                            become the XML tags in the prompt.

Workspace secrets (injected by the worker):
  LIGHTSEI_API_KEY    required; bot authenticates to Lightsei with this
  ANTHROPIC_API_KEY   required unless POLARIS_DRY_RUN=1
  POLARIS_GITHUB_TOKEN  required when POLARIS_GITHUB_REPO is set
"""

import base64
import fnmatch
import hashlib
import json
import os
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Optional

import lightsei


POLL_S = float(os.environ.get("POLARIS_POLL_S", "3600"))
MODEL = os.environ.get("POLARIS_MODEL", "claude-opus-4-7")
DOCS_DIR = Path(os.environ.get("POLARIS_DOCS_DIR", "."))
DRY_RUN = os.environ.get("POLARIS_DRY_RUN") == "1"
SYSTEM_PROMPT_PATH = Path(__file__).parent / "system_prompt.md"

# Phase 10.4: GitHub fetch path config. Resolved per-tick (not at
# import) so a worker secret-injection that arrives after import still
# takes effect on the next poll. Importing-time evaluation would lock
# in the values from the bot's first second of life.
_DEFAULT_DOCS_PATHS = "MEMORY.md,TASKS.md"


def _gh_config() -> Optional[dict]:
    """Return GitHub fetch config when fully populated, else None.

    All three of REPO + TOKEN are required; BRANCH defaults to 'main'
    and PATHS defaults to MEMORY.md + TASKS.md. Returning None means
    "fall back to the disk path" — the caller doesn't need to reason
    about partially-set state.
    """
    repo = os.environ.get("POLARIS_GITHUB_REPO", "").strip()
    token = os.environ.get("POLARIS_GITHUB_TOKEN", "").strip()
    if not repo or not token or "/" not in repo:
        return None
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return None
    branch = os.environ.get("POLARIS_GITHUB_BRANCH", "main").strip() or "main"
    paths_csv = os.environ.get(
        "POLARIS_GITHUB_DOCS_PATHS", _DEFAULT_DOCS_PATHS
    )
    paths = [p.strip() for p in paths_csv.split(",") if p.strip()]
    if not paths:
        paths = ["MEMORY.md", "TASKS.md"]
    return {
        "owner": owner,
        "name": name,
        "branch": branch,
        "token": token,
        "paths": paths,
    }


def _fetch_github_doc(
    *, owner: str, name: str, branch: str, path: str, token: str
) -> str:
    """GET /repos/{owner}/{name}/contents/{path}?ref={branch}.

    Uses the Contents API rather than the git-data tree+blob API
    because we want exactly one HTTP call per file and we know the
    paths in advance (no enumeration needed). Files up to ~1MB return
    base64-encoded content inline; anything larger requires a separate
    blob fetch — but Polaris docs (MEMORY.md / TASKS.md) are well
    inside the inline ceiling, so we don't bother with the larger-file
    branch.

    Raises GitHubDocFetchError on any non-2xx, transport error, or
    unexpected response shape. Caller is expected to swallow + skip
    the tick rather than crash — a transient outage shouldn't take
    Polaris down.
    """
    import httpx
    url = (
        f"https://api.github.com/repos/{owner}/{name}/contents/{path}"
        f"?ref={branch}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "lightsei-polaris",
    }
    try:
        with httpx.Client(timeout=15.0) as client:
            r = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise GitHubDocFetchError(
            f"network error fetching {path}: {type(exc).__name__}"
        ) from exc

    if r.status_code == 401:
        raise GitHubDocFetchError(
            f"GitHub rejected POLARIS_GITHUB_TOKEN (401) fetching {path}"
        )
    if r.status_code == 404:
        raise GitHubDocFetchError(
            f"GitHub returned 404 for {path} on {owner}/{name}@{branch}"
        )
    if not (200 <= r.status_code < 300):
        raise GitHubDocFetchError(
            f"GitHub returned {r.status_code} for {path}: "
            f"{(r.text or '')[:200]}"
        )

    body = r.json()
    if not isinstance(body, dict):
        # GET /contents/{path} returns a list when path is a directory.
        # Polaris docs are always files; this means the user pointed at
        # a directory by mistake.
        raise GitHubDocFetchError(
            f"unexpected response for {path}: got a directory listing, "
            "expected a file"
        )
    if body.get("type") != "file":
        raise GitHubDocFetchError(
            f"unexpected response shape for {path}: type={body.get('type')!r}"
        )
    encoding = body.get("encoding")
    content = body.get("content") or ""
    if encoding == "base64":
        try:
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubDocFetchError(
                f"failed to decode base64 content for {path}: {exc}"
            ) from exc
    raise GitHubDocFetchError(
        f"unsupported encoding {encoding!r} for {path}"
    )


class GitHubDocFetchError(Exception):
    """Raised by _fetch_github_doc on any failure. The bot's tick loop
    catches this and skips the tick (no plan emitted) so a transient
    GitHub blip doesn't crash the worker."""


def _hash16(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


# In-process change-detection state. Reset on every bot restart, so a
# redeploy always regenerates a plan against the current docs.
_last_hashes: Optional[dict] = None


def _read_docs_from_disk() -> dict:
    """Bundle-relative read. The deploy zip ships MEMORY.md + TASKS.md
    next to bot.py (and any other docs the user wants — but in
    practice nobody has asked for that yet, so disk mode stays
    hardcoded to those two)."""
    memory = (DOCS_DIR / "MEMORY.md").read_text()
    tasks = (DOCS_DIR / "TASKS.md").read_text()
    return {
        "docs": {"MEMORY.md": memory, "TASKS.md": tasks},
        "hashes": {
            "MEMORY.md": _hash16(memory),
            "TASKS.md": _hash16(tasks),
        },
    }


def _read_docs_from_github(cfg: dict) -> dict:
    """Fetch each path in cfg['paths'] via the Contents API. Hashes
    are computed from the decoded text, NOT the GitHub-reported `sha`,
    so the cache lines up with the disk-mode hashing scheme — pushing
    a doc with no real content change doesn't bust Polaris's cache."""
    docs: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for path in cfg["paths"]:
        text = _fetch_github_doc(
            owner=cfg["owner"],
            name=cfg["name"],
            branch=cfg["branch"],
            path=path,
            token=cfg["token"],
        )
        docs[path] = text
        hashes[path] = _hash16(text)
    return {"docs": docs, "hashes": hashes}


def _read_docs() -> dict:
    """Dispatch: GitHub if fully configured, else disk. Returns
    {docs: {filename: text}, hashes: {filename: short_sha}}."""
    cfg = _gh_config()
    if cfg is None:
        return _read_docs_from_disk()
    return _read_docs_from_github(cfg)


SUBMIT_PLAN_TOOL = {
    "name": "submit_plan",
    "description": (
        "Submit the orchestrator plan for the project. Call this tool exactly "
        "once with a structured plan. next_actions must contain 3 to 5 items; "
        "lead with the current NOW task and fill remaining slots with the "
        "obvious follow-ons in the active phase."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "1-2 sentences on the current project state. Lead with "
                    "the active phase number and what just shipped."
                ),
            },
            "next_actions": {
                "type": "array",
                "description": "3 to 5 next actions, ordered by priority.",
                "items": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": (
                                "The action. Cite phase / task numbers and "
                                "file paths when applicable."
                            ),
                        },
                        "why": {
                            "type": "string",
                            "description": (
                                "1-2 sentences on why this is the right next "
                                "step given the current state."
                            ),
                        },
                        "blocked_by": {
                            "anyOf": [{"type": "string"}, {"type": "null"}],
                            "description": (
                                "What blocks this action (a missing secret, "
                                "an upstream dependency, a decision), or "
                                "null if unblocked."
                            ),
                        },
                    },
                    "required": ["task", "why", "blocked_by"],
                    "additionalProperties": False,
                },
            },
            "parking_lot_promotions": {
                "type": "array",
                "description": (
                    "Parking-lot items that look ready to promote given the "
                    "current state. Empty list is fine if nothing stands out."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {"type": "string"},
                        "why": {"type": "string"},
                    },
                    "required": ["item", "why"],
                    "additionalProperties": False,
                },
            },
            "drift": {
                "type": "array",
                "description": (
                    "Real contradictions between MEMORY.md, TASKS.md, and "
                    "the Done Log. Stylistic differences don't count. "
                    "Empty list is fine."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "between": {
                            "type": "string",
                            "description": (
                                "Which files / sections the contradiction is "
                                "between, e.g. 'MEMORY.md vs TASKS.md'."
                            ),
                        },
                        "observation": {"type": "string"},
                    },
                    "required": ["between", "observation"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "summary",
            "next_actions",
            "parking_lot_promotions",
            "drift",
        ],
        "additionalProperties": False,
    },
}


def _call_claude(system_prompt: str, docs: dict) -> dict:
    """Calls Claude with a forced submit_plan tool call.

    `strict: true` + `tool_choice` to a specific tool guarantees the
    response contains exactly one tool_use block whose `input` matches
    the schema. No JSON parsing, no retry-on-parse-error path.
    """
    import anthropic

    # Wrap each doc in an XML tag named after its filename. The default
    # case (MEMORY.md + TASKS.md) lands the same prompt shape as before;
    # custom POLARIS_GITHUB_DOCS_PATHS just adds more tags.
    user_msg = "\n\n".join(
        f"<{filename}>\n{text}\n</{filename}>"
        for filename, text in docs["docs"].items()
    )
    client = anthropic.Anthropic()
    # Note: adaptive thinking is incompatible with `tool_choice` forcing a
    # specific tool (Opus 4.7 returns 400). For Polaris we want the
    # guaranteed schema match more than the visible reasoning, so we drop
    # thinking and rely on effort=high. If we ever want both, switch to
    # `tool_choice: {"type": "any"}` (still forces a tool call, but allows
    # thinking) — works because `submit_plan` is the only tool defined.
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        output_config={"effort": "high"},
        system=system_prompt,
        tools=[SUBMIT_PLAN_TOOL],
        tool_choice={"type": "tool", "name": "submit_plan"},
        messages=[{"role": "user", "content": user_msg}],
    )

    tool_block = next(
        (b for b in resp.content if b.type == "tool_use"
         and b.name == "submit_plan"),
        None,
    )
    if tool_block is None:
        # Forced tool_choice should make this unreachable, but guard anyway:
        # surface the stop_reason so it lands in logs / dashboard.
        raise RuntimeError(
            f"no submit_plan tool_use in response (stop_reason="
            f"{resp.stop_reason})"
        )

    return {
        "input": tool_block.input,
        "model": resp.model,
        "tokens_in": resp.usage.input_tokens,
        "tokens_out": resp.usage.output_tokens,
        "stop_reason": resp.stop_reason,
    }


@lightsei.track
def tick() -> None:
    global _last_hashes
    try:
        docs = _read_docs()
    except GitHubDocFetchError as exc:
        # GitHub couldn't serve the docs. Don't crash — log + skip the
        # tick. The next poll will retry. We deliberately do NOT treat
        # this as "docs unchanged" because we don't know that. Leave
        # _last_hashes as-is so the next successful fetch produces a
        # plan even if content matches the last cached hashes.
        print(f"github docs fetch failed: {exc}", flush=True)
        lightsei.emit(
            "polaris.tick_skipped",
            {"reason": "github fetch failed", "error": str(exc)},
        )
        return

    print(
        "docs: " + " ".join(
            f"{name}={h}" for name, h in docs["hashes"].items()
        ),
        flush=True,
    )

    if _last_hashes == docs["hashes"]:
        print("docs unchanged since last plan, skipping LLM call", flush=True)
        lightsei.emit(
            "polaris.tick_skipped",
            {"reason": "docs unchanged", "hashes": docs["hashes"]},
        )
        return

    if DRY_RUN:
        print("dry run: skipping Anthropic call", flush=True)
        lightsei.emit("polaris.tick_dry_run", {"hashes": docs["hashes"]})
        _last_hashes = docs["hashes"]
        return

    system_prompt = SYSTEM_PROMPT_PATH.read_text()
    result = _call_claude(system_prompt, docs)
    plan = result["input"]

    payload = {
        # Pretty-print the structured input so the dashboard's "raw response"
        # expander has something readable. The structured fields below are
        # the source of truth for rendering.
        "text": json.dumps(plan, indent=2),
        "doc_hashes": docs["hashes"],
        "model": result["model"],
        "tokens_in": result["tokens_in"],
        "tokens_out": result["tokens_out"],
        "summary": plan["summary"],
        "next_actions": plan["next_actions"],
        "parking_lot_promotions": plan["parking_lot_promotions"],
        "drift": plan["drift"],
    }

    print(
        f"plan: {len(plan['next_actions'])} actions, "
        f"{len(plan['parking_lot_promotions'])} promotions, "
        f"{len(plan['drift'])} drift items "
        f"({result['tokens_in']} in / {result['tokens_out']} out)",
        flush=True,
    )

    lightsei.emit("polaris.plan", payload)
    _last_hashes = docs["hashes"]


# ---------- Phase 11.5: react to push events ---------- #
#
# When the GitHub webhook fires, the backend enqueues a
# `polaris.evaluate_push` command for this bot. The handler reads
# POLARIS_PUSH_RULES (a comma-separated list of `<glob>:<command_kind>`
# entries) and dispatches one command of `command_kind` per rule that
# matches at least one touched path. Default rules cover the obvious
# cases for this repo (backend or polaris source touched → run tests);
# every other rule must be added explicitly so the cost stays bounded.
#
# This path deliberately does NOT call Claude. Hourly tick still does
# the LLM-driven planning; evaluate_push is the cheap event-driven
# complement that lights up dispatch chains for free.

DEFAULT_PUSH_RULES = "backend/**:atlas.run_tests,polaris/**:atlas.run_tests"


def _parse_push_rules(raw: Optional[str]) -> list[tuple[str, str]]:
    """Parse `<glob>:<kind>,<glob>:<kind>` into a list of pairs.

    Whitespace around entries / fields is tolerated. Entries missing the
    `:kind` half or with empty halves are dropped silently — a malformed
    rule shouldn't crash the bot's tick path. An empty / unset env var
    falls back to DEFAULT_PUSH_RULES.
    """
    if raw is None or not raw.strip():
        raw = DEFAULT_PUSH_RULES
    rules: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or ":" not in entry:
            continue
        pattern, _, kind = entry.partition(":")
        pattern = pattern.strip()
        kind = kind.strip()
        if pattern and kind:
            rules.append((pattern, kind))
    return rules


def _glob_matches_any(pattern: str, paths: list[str]) -> bool:
    """fnmatch with `**` semantics (path-component-aware).

    fnmatch alone treats `*` as matching any chars including `/`, which
    is what we want for `polaris/**` to match `polaris/bot.py`. We add
    the explicit `**` → `*` collapse so users can write `backend/**`
    naturally and it does what they expect.
    """
    norm = pattern.replace("**", "*")
    return any(fnmatch.fnmatch(p, norm) for p in paths)


def evaluate_push(
    payload: dict[str, Any],
    *,
    rules_env: Optional[str] = None,
    send_command: Any = None,
) -> dict[str, Any]:
    """Pure handler core. Returns a summary of what was dispatched.

    Factored out of the @on_command wrapper so unit tests can drive it
    with a mock send_command. The wrapper just hands the SDK function
    in. Returns:
      {
        "matched_rules": [{"pattern", "kind", "matched_paths": [...]}],
        "dispatched": [{"target_agent": "atlas", "kind", "command_id"}],
        "touched_paths": [...],
        "skipped_reason": str | None,
      }
    """
    if send_command is None:
        send_command = lightsei.send_command

    touched_paths = payload.get("touched_paths") or []
    if not isinstance(touched_paths, list):
        touched_paths = []
    paths: list[str] = [p for p in touched_paths if isinstance(p, str)]

    raw = rules_env if rules_env is not None else os.environ.get(
        "POLARIS_PUSH_RULES"
    )
    rules = _parse_push_rules(raw)

    matched: list[dict[str, Any]] = []
    dispatched: list[dict[str, Any]] = []
    commit_sha = payload.get("commit_sha") or ""
    branch = payload.get("branch") or ""

    for pattern, kind in rules:
        matching = [p for p in paths if _glob_matches_any(pattern, [p])]
        if not matching:
            continue
        # Phase 11.5 ships only one downstream target (atlas) so the
        # rule's `kind` directly identifies the agent: `<agent>.<verb>`.
        # Future rule kinds with different targets can introduce a
        # third field; keep the parsing forward-compat by splitting on
        # the first dot.
        target_agent = kind.split(".", 1)[0]
        cmd_payload = {
            "commit_sha": commit_sha,
            "branch": branch,
            "trigger": "polaris.evaluate_push",
            "matched_pattern": pattern,
            "matched_paths": matching,
        }
        # `source_agent=` is a Phase-11.5+ SDK kwarg. On PyPI 0.1.0 it
        # raises TypeError; in that case dispatch without the flag —
        # the chain_id still propagates via the SDK's thread-local
        # context that the auto-poller set when it claimed this command.
        # Source-agent attribution gets dropped, which means the 11.6
        # constellation edges won't carry "from polaris" data on this
        # path, but the chain itself works end-to-end.
        try:
            cmd = send_command(
                target_agent, kind, cmd_payload, source_agent="polaris"
            )
        except TypeError:
            cmd = send_command(target_agent, kind, cmd_payload)
        matched.append(
            {"pattern": pattern, "kind": kind, "matched_paths": matching}
        )
        dispatched.append(
            {
                "target_agent": target_agent,
                "kind": kind,
                "command_id": (cmd or {}).get("id"),
            }
        )

    skipped_reason: Optional[str] = None
    if not paths:
        skipped_reason = "no touched paths in push payload"
    elif not rules:
        skipped_reason = "POLARIS_PUSH_RULES is empty"
    elif not matched:
        skipped_reason = "no rule matched any touched path"

    return {
        "matched_rules": matched,
        "dispatched": dispatched,
        "touched_paths": paths,
        "skipped_reason": skipped_reason,
    }


@lightsei.on_command(
    "polaris.evaluate_push",
    description=(
        "Heuristic dispatch on a GitHub push. Reads POLARIS_PUSH_RULES "
        "and fires one downstream command per matching rule. No LLM call."
    ),
)
def _handle_evaluate_push(payload: dict[str, Any]) -> dict[str, Any]:
    summary = evaluate_push(payload)
    print(
        f"evaluate_push: touched={len(summary['touched_paths'])} "
        f"matched={len(summary['matched_rules'])} "
        f"dispatched={len(summary['dispatched'])} "
        f"skipped_reason={summary['skipped_reason']!r}",
        flush=True,
    )
    return summary


def main() -> None:
    api_key = os.environ.get("LIGHTSEI_API_KEY")
    base_url = os.environ.get("LIGHTSEI_BASE_URL", "https://api.lightsei.com")
    agent_name = os.environ.get("LIGHTSEI_AGENT_NAME", "polaris")

    if not api_key:
        print("LIGHTSEI_API_KEY not set; can't ingest events", flush=True)
        sys.exit(1)

    lightsei.init(
        api_key=api_key,
        agent_name=agent_name,
        version="0.1.0",
        base_url=base_url,
    )

    cfg = _gh_config()
    if cfg is not None:
        docs_source = (
            f"github={cfg['owner']}/{cfg['name']}@{cfg['branch']} "
            f"paths={','.join(cfg['paths'])}"
        )
    else:
        docs_source = f"disk={DOCS_DIR.resolve()}"

    print(
        f"polaris up: agent={agent_name} model={MODEL} poll={POLL_S}s "
        f"{docs_source} dry_run={DRY_RUN}",
        flush=True,
    )

    while True:
        try:
            tick()
        except Exception:
            print(f"tick crashed:\n{traceback.format_exc()}", flush=True)
        lightsei.flush(timeout=2.0)
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
