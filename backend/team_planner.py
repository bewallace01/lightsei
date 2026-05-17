"""Phase 12C.1: project-analysis layer.

Takes a README / repo URL / freeform description and asks Claude to
propose a non-overlapping team of 3-7 bots, each named from the
`STAR_DICTIONARY` and wired into a bounded dispatch graph. The plan
endpoint runs this; 12C.2 visualizes it; 12C.3-12C.4 generate +
deploy each bot via 12B's per-bot pipeline.

Pure module: prompt construction + tool schema + validation. No HTTP,
no DB, no Anthropic client. The endpoint in main.py owns those.
"""
from typing import Any

from agent_generator import (
    STAR_DICTIONARY,
    is_valid_star_name,
    render_star_dictionary_for_prompt,
)


# Soft caps the LLM is told to respect. The schema's `minItems` /
# `maxItems` enforce the hard bounds; these are the recommended range.
MIN_TEAM_SIZE = 3
MAX_TEAM_SIZE = 7

# Each bot's outgoing dispatch edges. The spec is "at most one or two
# outgoing edges (avoid spaghetti)" — we cap at 2 in the tool schema and
# tell the model to prefer 1.
MAX_DISPATCHES_PER_BOT = 2


# ---------- Submit-team tool schema ---------- #
#
# Forced tool_choice on this schema = guaranteed-shape JSON output.
# Same trick as 12B.1's `submit_bot` — no JSON-parsing retries.

SUBMIT_TEAM_TOOL: dict[str, Any] = {
    "name": "submit_team",
    "description": (
        f"Submit a roster of {MIN_TEAM_SIZE}-{MAX_TEAM_SIZE} Lightsei bots "
        "that together would maintain the project the user described. Each "
        "member's name MUST come from the star-naming dictionary in the "
        "system prompt; each member's role must not overlap with the "
        f"others; the dispatch graph must be bounded (at most "
        f"{MAX_DISPATCHES_PER_BOT} outgoing edges per bot, prefer one)."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "rationale": {
                "type": "string",
                "description": (
                    "1-3 sentences on why this team fits the project — what "
                    "recurring kinds of work it covers and which dispatch "
                    "edges tie it together. Written for the user to skim."
                ),
            },
            # Anthropic's strict mode rejects `minItems`/`maxItems` values
            # other than 0 or 1, so we encode the bounds in the description
            # text instead. `validate_team_plan()` enforces them on the
            # response, with a corrective retry turn when the model bursts
            # the cap — same loop we already had for bad names.
            "team": {
                "type": "array",
                "description": (
                    f"Between {MIN_TEAM_SIZE} and {MAX_TEAM_SIZE} bots, "
                    "inclusive. The endpoint rejects + retries if you go "
                    "outside this range."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": (
                                "A name from the star-naming dictionary in "
                                "the system prompt. Must not be a name "
                                "already taken in the workspace, and must "
                                "not collide with another member of this "
                                "team."
                            ),
                        },
                        "role": {
                            "type": "string",
                            "enum": ["orchestrator", "specialist", "messenger"],
                            "description": (
                                "Role bucket used by the dashboard's "
                                "constellation map. Most bots are "
                                "specialists; pick orchestrator only for a "
                                "team-coordinator (Polaris-shaped) and "
                                "messenger only for outbound notifiers "
                                "(Hermes-shaped)."
                            ),
                        },
                        "summary": {
                            "type": "string",
                            "description": (
                                "One-line description of what this bot does. "
                                "Surfaces on the constellation preview as "
                                "the bot's hover text."
                            ),
                        },
                        "command_kinds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Symbolic kinds this bot handles via "
                                "`@lightsei.on_command(...)`. Convention: "
                                "`<agent_name>.<verb>` (e.g. `argus.scan`, "
                                "`hermes.post`). Empty array for cron-style "
                                "bots that only tick on a schedule."
                            ),
                        },
                        "dispatches_to": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Names (from this team or the existing "
                                "constellation) this bot dispatches commands "
                                f"to. At most {MAX_DISPATCHES_PER_BOT} "
                                "entries — prefer 0 or 1 outgoing edges; "
                                f"{MAX_DISPATCHES_PER_BOT} only when both "
                                "targets serve genuinely different purposes. "
                                "Avoid spaghetti graphs — a linear chain "
                                "is more debuggable than a fanout. The "
                                "endpoint rejects + retries if you exceed "
                                "the cap."
                            ),
                        },
                        "needs_workspace_secrets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Workspace secrets this bot needs at runtime, "
                                "e.g. ['GITHUB_TOKEN', 'SLACK_WEBHOOK_URL']. "
                                "The team-review UI surfaces these so the "
                                "user can set missing ones before deploy."
                            ),
                        },
                        "draft_description": {
                            "type": "string",
                            "description": (
                                "A paragraph the 12B per-bot generator can "
                                "consume as its `description` input. Should "
                                "include what the bot does, when it runs "
                                "(reactive to commands or cron-style with "
                                "what cadence), and which other team "
                                "members it coordinates with."
                            ),
                        },
                    },
                    "required": [
                        "name",
                        "role",
                        "summary",
                        "command_kinds",
                        "dispatches_to",
                        "needs_workspace_secrets",
                        "draft_description",
                    ],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["rationale", "team"],
        "additionalProperties": False,
    },
}


# ---------- System prompt ---------- #


_TEAM_DESIGN_GUIDANCE = """
# How to design a Lightsei team

Read the project's README + freeform description and identify the
*recurring kinds of work* it would benefit from automating. Common
buckets:

- Testing: run the test suite on each push, report results.
- Security: scan for hardcoded secrets, vulnerable deps, suspicious diffs.
- Deploy verification: ping a deployed URL post-merge, alert on regression.
- PR review: structural critique on open PRs.
- On-call / paging: alert on production errors / outages.
- Doc maintenance: keep README / changelog / TASKS in sync with the code.
- Content moderation: filter user-submitted text on a hosted product.
- Cost auditing: surface where dollars went, what was wasted.
- Summarization: weekly digest of activity, recap emails.

Pick 3-7 bots, one per bucket the project actually needs. Don't propose
a bot for every bucket — propose only ones that make sense for THIS
project. A static-site repo doesn't need a deploy verifier; a backend
service doesn't need a content moderator unless it explicitly does
user-submitted content.

## Role buckets

- `orchestrator`: at most one. Plans + coordinates. Polaris-shaped.
  Cron-style ticking, reads docs, dispatches to specialists. Don't
  propose a new orchestrator if Polaris is already in the workspace.
- `messenger`: outbound notifiers. Hermes-shaped. Receives a single
  command kind (e.g. `hermes.post`) and pushes to Slack / email /
  webhook. Don't propose two messengers.
- `specialist`: everyone else. Argus, Vega, Atlas, etc.

## Dispatch graph

- Prefer linear chains over fanouts. `polaris -> argus -> hermes` is
  easier to reason about than `polaris -> {argus, vega, sirius}`.
- Each bot should have at most 2 outgoing edges; 1 is the sweet spot.
- A messenger has zero outgoing edges (it's a leaf).
- An orchestrator typically dispatches to 1-2 specialists.
- Specialists either dispatch to a single messenger OR to the next
  specialist in a chain. They rarely fan out.

## Names

Pick from the star-naming dictionary in the system prompt. Names
already in this workspace are filtered out so you don't propose a
collision. Pick the name whose theme/role best matches the bot's job
— `argus` (the all-seeing giant) for security, `vega` (sharp + bright)
for code review, `hermes` (messenger) for outbound posts, etc. Don't
reuse a name within the team.

## Reusing existing agents

If the workspace already has bots, the system prompt lists them. When
your proposed work overlaps with an existing bot, **don't** propose a
duplicate — instead, wire your new bots to dispatch to the existing
one. Include the existing agent's name in `dispatches_to` rather than
adding it to `team`.
"""


def _format_existing_agents(agents: list[dict[str, Any]]) -> str:
    """Render the workspace's existing agents into prompt-ready bullets."""
    if not agents:
        return "_(empty workspace — design from scratch)_"
    lines = []
    for a in agents:
        name = a.get("name", "?")
        role = a.get("role") or "unknown role"
        kinds = a.get("command_kinds") or []
        kinds_str = (
            f" — handles: {', '.join(kinds)}" if kinds else ""
        )
        lines.append(f"- `{name}` ({role}){kinds_str}")
    return "\n".join(lines)


def build_system_prompt(
    *,
    existing_agents: list[dict[str, Any]],
    reserved_names: set[str],
) -> str:
    """Assemble the system prompt. `existing_agents` is the workspace's
    current roster (with optional command_kinds enrichment); their names
    are auto-merged into `reserved_names` for the star table."""
    merged_reserved = set(reserved_names)
    for a in existing_agents:
        n = a.get("name")
        if isinstance(n, str):
            merged_reserved.add(n.strip().lower())

    star_table = render_star_dictionary_for_prompt(merged_reserved)
    agents_block = _format_existing_agents(existing_agents)

    return f"""You analyze a project description and propose a team of \
Lightsei bots that together would maintain it.

A Lightsei bot is a single Python file deployed in the user's workspace.
Bots can either tick on a schedule (like a cron job) or react to commands
dispatched by other bots. They emit events the dashboard renders. Bots
named in your output will be generated as actual code in a later step
(Phase 12C.3) — your job here is the team plan, not the source code.

{_TEAM_DESIGN_GUIDANCE}

# This workspace's existing agents

{agents_block}

# Star-naming dictionary

The `name` for every team member MUST come from this list. Names
already in the workspace are filtered out. Pick the row whose
theme/role best matches what each bot will do.

{star_table}

# Output

Call the `submit_team` tool exactly once with:
- `rationale`: 1-3 sentences on why this team fits the project.
- `team`: 3-7 members, each with name, role, summary, command_kinds,
  dispatches_to, needs_workspace_secrets, draft_description.

The `draft_description` for each member will feed into the per-bot
generator (Phase 12B) verbatim, so write it as if instructing the
generator: what the bot does, when it runs, which other team members
it coordinates with.
"""


def build_user_message(
    *,
    readme_text: str | None,
    freeform_description: str | None,
    github_repo: str | None,
) -> str:
    """Concatenate the inputs into one user turn. At least one of the
    three must be non-empty (the endpoint enforces this)."""
    parts: list[str] = []
    if github_repo:
        parts.append(f"GitHub repo: `{github_repo}`")
    if readme_text:
        parts.append(f"README:\n\n{readme_text.strip()}")
    if freeform_description:
        parts.append(
            f"Additional context from the user:\n\n{freeform_description.strip()}"
        )
    return "\n\n---\n\n".join(parts)


# ---------- Plan validation ---------- #


def validate_team_plan(
    plan: dict[str, Any], reserved_names: set[str]
) -> list[str]:
    """Return a list of human-readable problems with the plan, empty if
    it's valid. The endpoint surfaces problems in a corrective retry
    turn the LLM can address.

    Checks:
      - team length within [MIN_TEAM_SIZE, MAX_TEAM_SIZE]
      - every name is in STAR_DICTIONARY
      - no name collides with an existing workspace agent
      - no two team members share a name
      - dispatches_to references either the team itself or a reserved
        agent (the existing roster) — dangling edges are flagged
      - at most one orchestrator
    """
    problems: list[str] = []
    team = plan.get("team")
    if not isinstance(team, list):
        return ["`team` must be an array"]

    if len(team) < MIN_TEAM_SIZE or len(team) > MAX_TEAM_SIZE:
        problems.append(
            f"team has {len(team)} members; must be between "
            f"{MIN_TEAM_SIZE} and {MAX_TEAM_SIZE}"
        )

    reserved_lower = {n.strip().lower() for n in reserved_names}
    seen_names: set[str] = set()
    orchestrator_count = 0

    for i, m in enumerate(team):
        if not isinstance(m, dict):
            problems.append(f"team[{i}] is not an object")
            continue
        name_raw = m.get("name") or ""
        name = name_raw.strip().lower()

        if not is_valid_star_name(name):
            problems.append(
                f"team[{i}].name `{name_raw}` is not in the star-naming dictionary"
            )
        if name in reserved_lower:
            problems.append(
                f"team[{i}].name `{name_raw}` is already in use in the workspace"
            )
        if name in seen_names:
            problems.append(
                f"team[{i}].name `{name_raw}` is used twice within the team"
            )
        seen_names.add(name)

        if m.get("role") == "orchestrator":
            orchestrator_count += 1

    if orchestrator_count > 1:
        problems.append(
            f"at most one orchestrator allowed; got {orchestrator_count}"
        )

    # Dispatch graph: every target must be either a team member or a
    # known existing agent. Dangling edges are usually a hallucination.
    valid_targets = seen_names | reserved_lower
    for i, m in enumerate(team):
        if not isinstance(m, dict):
            continue
        targets = m.get("dispatches_to") or []
        if not isinstance(targets, list):
            continue
        if len(targets) > MAX_DISPATCHES_PER_BOT:
            problems.append(
                f"team[{i}].dispatches_to has {len(targets)} edges; "
                f"max is {MAX_DISPATCHES_PER_BOT}"
            )
        for t in targets:
            if not isinstance(t, str):
                continue
            if t.strip().lower() not in valid_targets:
                problems.append(
                    f"team[{i}].dispatches_to references `{t}`, which is "
                    "neither a team member nor an existing agent"
                )

    return problems


def build_validation_retry_message(problems: list[str]) -> str:
    """Compose a corrective retry turn for the LLM."""
    bullet = "\n".join(f"- {p}" for p in problems)
    return (
        "The team plan you submitted has the following issues:\n\n"
        f"{bullet}\n\n"
        "Call submit_team again with a corrected plan. Keep the rationale "
        "format the same."
    )


def run_team_plan_job(
    session,
    workspace_id: str,
    payload: dict,
) -> dict:
    """Job-runner handler for `kind='team_plan'`.

    Mirrors the original `POST /workspaces/me/teams/plan` body one-for-one,
    minus the synchronous input gate (the endpoint did that before
    enqueue). Returns the same dict the endpoint used to return
    synchronously; the runner persists it to `generation_jobs.result_payload`.

    The GitHub README fetch moves into the handler so a slow fetch doesn't
    block the enqueue. The endpoint still validates the input shape
    (`_parse_github_repo`) so users see format errors synchronously; the
    network call to GitHub happens here.

    Failure modes that used to surface as HTTPException still raise
    HTTPException; the runner catches and writes `error`. The dashboard's
    poll loop surfaces the error text verbatim.
    """
    import uuid
    from datetime import datetime, timezone
    from decimal import Decimal
    from typing import Any, Optional

    import anthropic
    from fastapi import HTTPException
    from sqlalchemy import select

    import github_api
    import secrets_crypto
    from cost import workspace_cost_mtd
    from db import ensure_agent
    from models import Agent, GitHubIntegration, Run, WorkspaceSecret
    from pricing import compute_cost_usd

    # Local copy of main._anthropic_error_to_http (same reason as
    # agent_generator.run_agent_generation_job: avoid importing main).
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
    # presence; a race (revoked between enqueue and run) is possible.
    secret_row = session.get(WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY"))
    if secret_row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ANTHROPIC_API_KEY was removed from this workspace between "
                "enqueue and run. Re-add it on /account and retry."
            ),
        )
    try:
        anthropic_key = secrets_crypto.decrypt(secret_row.encrypted_value)
    except Exception:
        raise HTTPException(
            status_code=500,
            detail="failed to decrypt ANTHROPIC_API_KEY (server config issue)",
        )

    readme_text = payload.get("readme_text") or None
    freeform_description = payload.get("freeform_description") or None
    github_repo = payload.get("github_repo") or None
    github_branch = payload.get("github_branch") or None
    # Endpoint normalized `github_repo` to a parsed `(owner, name)` tuple
    # before enqueue; if it's set, the handler uses that. Falls back to
    # parsing again here so an older enqueued row (or a direct caller)
    # still works.
    parsed_repo = payload.get("github_repo_parsed")

    # 2. Resolve README from GitHub if requested. Done in the handler so
    # the fetch is off the request path. Format-level errors already
    # raised synchronously in the endpoint via `_parse_github_repo`.
    if github_repo and not readme_text:
        if parsed_repo and isinstance(parsed_repo, list) and len(parsed_repo) == 2:
            owner, name = parsed_repo[0], parsed_repo[1]
        else:
            owner, name, _err = _parse_github_repo_safe(github_repo)
            if _err is not None:
                raise HTTPException(status_code=400, detail=_err)
        pat: Optional[str] = None
        integ = session.execute(
            select(GitHubIntegration).where(
                GitHubIntegration.workspace_id == workspace_id,
                GitHubIntegration.repo_owner == owner,
                GitHubIntegration.repo_name == name,
            )
        ).scalar_one_or_none()
        if integ is not None and integ.encrypted_pat:
            try:
                pat = secrets_crypto.decrypt(integ.encrypted_pat)
            except Exception:
                pat = None
        try:
            readme_text = github_api.fetch_readme(
                repo_owner=owner,
                repo_name=name,
                branch=github_branch,
                pat=pat,
            )
        except github_api.GitHubAPIError as e:
            raise HTTPException(
                status_code=400 if e.kind in ("auth", "not_found") else 502,
                detail=f"GitHub: {e.message}",
            )

    # 3. Snapshot the workspace's existing agents so the plan can wire
    # to them rather than duplicate. Mirrors the agent_generator path.
    agent_rows = session.execute(
        select(Agent).where(Agent.workspace_id == workspace_id).order_by(Agent.name)
    ).scalars().all()
    existing_agents: list[dict[str, Any]] = []
    reserved_names: set[str] = set()
    for a in agent_rows:
        if a.name.startswith("lightsei."):
            continue
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
                "command_kinds": cmd_kinds,
            }
        )

    system_prompt = build_system_prompt(
        existing_agents=existing_agents,
        reserved_names=reserved_names,
    )
    user_msg = build_user_message(
        readme_text=readme_text,
        freeform_description=freeform_description,
        github_repo=github_repo,
    )

    # Now off the request path; keep max_retries=5 so transient 529s
    # don't bubble as job failures.
    client = anthropic.Anthropic(api_key=anthropic_key, max_retries=5)
    model = "claude-opus-4-7"
    token_log: list[tuple[str, int, int]] = []

    def _ask(extra_user_msg: Optional[str] = None) -> Any:
        messages: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
        if extra_user_msg:
            messages.append({"role": "user", "content": extra_user_msg})
        resp = client.messages.create(
            model=model,
            max_tokens=8000,
            system=system_prompt,
            tools=[SUBMIT_TEAM_TOOL],
            tool_choice={"type": "tool", "name": "submit_team"},
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
                and getattr(b, "name", None) == "submit_team"
            ),
            None,
        )
        if block is None:
            raise HTTPException(
                status_code=502,
                detail=(
                    "model did not call submit_team "
                    f"(stop_reason={getattr(resp, 'stop_reason', '?')})"
                ),
            )
        return block.input

    try:
        try:
            resp = _ask()
            plan = _extract(resp)
        except anthropic.APIError as e:
            raise _anthropic_err_to_http(e)

        problems = validate_team_plan(plan, reserved_names)
        if problems:
            retry_msg = build_validation_retry_message(problems)
            try:
                resp = _ask(extra_user_msg=retry_msg)
                plan = _extract(resp)
            except anthropic.APIError as e:
                raise _anthropic_err_to_http(e)
            remaining = validate_team_plan(plan, reserved_names)
            if remaining:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "team planner could not produce a valid plan after "
                        "retry; remaining issues: " + " | ".join(remaining)
                    ),
                )

        return {
            "rationale": plan.get("rationale") or "",
            "team": plan.get("team") or [],
            "model_used": getattr(resp, "model", model),
            "tokens_in": getattr(getattr(resp, "usage", None), "input_tokens", None),
            "tokens_out": getattr(getattr(resp, "usage", None), "output_tokens", None),
        }
    finally:
        # Spend lands on `lightsei.system` even on error paths: Anthropic
        # billed for whatever tokens loaded before the exception. Commit
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


def _parse_github_repo_safe(s: str) -> tuple[str, str, str | None]:
    """Parse `owner/name` or a full URL, returning (owner, name, error).

    Mirrors main._parse_github_repo's logic but returns an error string
    instead of raising. Used by the job handler when an older enqueued
    row didn't pre-parse; the endpoint uses the strict raising version.
    """
    raw = s.strip()
    if raw.endswith(".git"):
        raw = raw[:-4]
    for prefix in ("https://github.com/", "http://github.com/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    if raw.startswith("git@github.com:"):
        raw = raw[len("git@github.com:"):]
    parts = raw.strip("/").split("/")
    if len(parts) < 2:
        return "", "", f"github_repo must be `owner/name` or a GitHub URL (got {s!r})"
    return parts[0], parts[1], None


def _register() -> None:
    import jobs
    jobs.register_handler("team_plan", run_team_plan_job)


_register()
