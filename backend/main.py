import hashlib
import hmac
import json
import os
import secrets as _stdlib_secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, func, select, text
from sqlalchemy.orm import Session

import limits
import policies
import secrets_crypto
from auth import AuthResult, get_authenticated, get_workspace_id
from cost import (
    add_run_cost_from_event,
    agent_cost_since,
    utc_day_start,
    workspace_cost_mtd,
)
from pricing import compute_cost_usd
from db import ensure_agent, get_session, session_scope
from validator_defaults import seed_default_validators
from limits import (
    BodySizeLimitMiddleware,
    limit_login_attempt,
    limit_signup_attempt,
)
from keys import (
    generate_key,
    generate_session_token,
    hash_token,
    prefix_for_display,
)
from migrate import upgrade_to_head
from models import (
    Agent,
    AgentInstance,
    ApiKey,
    Command,
    CommandAutoApprovalRule,
    Deployment,
    DeploymentBlob,
    DeploymentLog,
    EmailSigninToken,
    Event,
    EventValidation,
    GenerationJob,
    GitHubAgentPath,
    GitHubIntegration,
    NotificationChannel,
    ConnectorInstallation,
    ConnectorOAuthPendingState,
    NotificationDelivery,
    OAuthPendingState,
    Run,
    SlackChannel,
    SlackEvent,
    SlackOAuthPendingState,
    SlackWorkspace,
    Session as SessionRow,
    Thread,
    ThreadMessage,
    User,
    ValidatorConfig,
    Workspace,
    WorkspaceSecret,
)
import github_api
import notifications
import validators
from notifications import triggers as notification_triggers
from validation_pipeline import (
    evaluate_validators,
    find_blocking_failures,
    write_validation_rows,
)
from worker_auth import get_worker
from passwords import hash_password, verify_password

SESSION_TTL = timedelta(days=30)
COMMAND_TTL = timedelta(hours=24)
# An instance is "active" if we heard from it within this window. Tuned for a
# default 30s SDK heartbeat with two missed beats of slack.
INSTANCE_ACTIVE_WINDOW = timedelta(seconds=90)
# Cap on concurrently-active instances of the same agent from a single
# hostname. Stops the runaway-process pattern where someone leaves
# `python polaris/bot.py` running in 25 detached terminal tabs and each
# one independently bills Anthropic. The 26th refuses to register and
# the SDK exits with a clear message rather than silently overlapping.
# Override per-deployment with LIGHTSEI_MAX_INSTANCES_PER_HOSTNAME.
MAX_INSTANCES_PER_HOSTNAME = int(
    os.getenv("LIGHTSEI_MAX_INSTANCES_PER_HOSTNAME", "3")
)
# A worker that hasn't heartbeated for this long loses its claim — any other
# worker can re-claim the deployment. Tuned for a 30s worker heartbeat.
WORKER_CLAIM_TTL = timedelta(seconds=120)
# Max upload bytes for a deployment zip. Mirrors limits.MAX_UPLOAD_BYTES so
# the per-route check matches the middleware's cap.
MAX_DEPLOYMENT_BLOB_BYTES = 10 * 1024 * 1024
# A single deployment keeps the most recent N log lines on the server.
# Older lines are pruned on insert.
MAX_DEPLOYMENT_LOG_LINES = 1000


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventIn(BaseModel):
    run_id: str
    agent_name: str
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[datetime] = None


class PolicyCheckIn(BaseModel):
    agent_name: Optional[str] = None
    run_id: Optional[str] = None
    action: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentPatchIn(BaseModel):
    daily_cost_cap_usd: Optional[float] = None  # null clears the cap
    system_prompt: Optional[str] = None  # null clears the prompt
    # Phase 12.1: per-agent LLM provider + model pin. null clears.
    # Provider validated against the small enum below at handler time.
    provider: Optional[str] = None
    model: Optional[str] = None
    # Per-agent tick interval (seconds). null = use the bot's env default.
    # Validated against the bounds below at handler time so a typo in the
    # dashboard doesn't melt budget by setting a 1-second tick.
    tick_interval_s: Optional[int] = None
    # Short freeform description shown on the /agents roster. null clears.
    description: Optional[str] = Field(default=None, max_length=2000)
    # Phase 16.1: trust-zone sensitivity. Validated against
    # _VALID_SENSITIVITY_LEVELS in the handler.
    sensitivity_level: Optional[str] = None
    # Phase 16.4: opt-in for cross-zone dispatch. None = leave unchanged;
    # True/False updates the column.
    dispatches_cross_zone: Optional[bool] = None
    # Distinguish "field not provided" from "explicitly null". Pydantic v2:
    # we'll detect via model_fields_set.


# Tick-interval bounds. 60s lower bound: anything tighter will rate-limit
# against vendor LLM APIs and burn budget. 86400s (24h) upper bound: longer
# intervals than that should use a different mechanism (cron, scheduled
# routine), not a tick loop.
AGENT_TICK_INTERVAL_MIN_S = 60
AGENT_TICK_INTERVAL_MAX_S = 86400


# Providers Lightsei has (or plans to have, in 12.2/12.3) an SDK adapter for.
# Validating at the API layer rather than a DB CHECK constraint so a future
# adapter ships without a schema migration. Keeping the set tight on input
# prevents typos like "antropic" from silently sticking.
SUPPORTED_PROVIDERS: set[str] = {
    "openai",
    "anthropic",
    "google",
    "groq",
    "xai",
    "cohere",
}


class WorkspaceCreateIn(BaseModel):
    name: str
    api_key_name: str = "default"


class WorkspacePatchIn(BaseModel):
    # name and budget are independently optional — clients can patch one
    # without touching the other. Pydantic v2's `model_fields_set`
    # distinguishes "client didn't send this" from "client sent null."
    name: Optional[str] = Field(default=None, min_length=1)
    # NULL means "clear the cap" when the client explicitly passes null;
    # the patch handler reads `model_fields_set` to disambiguate.
    budget_usd_monthly: Optional[float] = None


class ApiKeyCreateIn(BaseModel):
    name: str = "default"


class SignupIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    workspace_name: str = Field(min_length=1)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


# Phase 17.2: magic-link auth.
class MagicLinkRequestIn(BaseModel):
    email: EmailStr


class MagicLinkConsumeIn(BaseModel):
    token: str = Field(min_length=8, max_length=256)


class CommandEnqueueIn(BaseModel):
    kind: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    # Phase 11.2: dispatch chain wiring. The SDK in 11.1 already
    # sends dispatch_chain_id on every send_command call (Pydantic
    # silently dropped it before this phase); now the server reads
    # it. source_agent identifies the dispatcher when present —
    # NULL means "enqueued by a user / off-platform integration"
    # which is fine and bypasses the depth + per-day caps because
    # the runaway risk only exists for agent-driven chains.
    dispatch_chain_id: Optional[str] = None
    source_agent: Optional[str] = None


class CommandApprovalIn(BaseModel):
    """Phase 11.2: shape for POST /commands/{id}/approve and /reject.
    `reason` is optional free-text recorded on the audit trail. The
    handler differentiates approve vs reject by the path, not the
    payload — keeps the action explicit at the URL."""
    reason: Optional[str] = Field(default=None, max_length=500)


class AutoApprovalRuleIn(BaseModel):
    source_agent: str = Field(min_length=1, max_length=64)
    target_agent: str = Field(min_length=1, max_length=64)
    command_kind: str = Field(min_length=1, max_length=128)
    mode: str = Field(pattern="^(auto_approve|require_human)$")


class NotificationDispatchIn(BaseModel):
    """Phase 11.4: Hermes uses this endpoint to fan out arbitrary
    text from other agents into the workspace's configured channels."""
    channel_name: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=4000)
    severity: str = Field(
        default="info", pattern="^(info|error|warning)$"
    )


class CommandCompleteIn(BaseModel):
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class AgentManifestIn(BaseModel):
    command_handlers: list[dict[str, Any]] = Field(default_factory=list)


class AgentGenerateIn(BaseModel):
    """Input to `POST /workspaces/me/agents/generate`.

    `description` (Phase 12B.1) is the user's natural-language prompt — what
    the bot should do. `target_agents` is an optional whitelist of existing
    agents to encourage coordination with (the LLM still sees the full
    workspace constellation either way; this is a hint). `name_hint` lets
    the user nudge toward a specific star-dictionary name, which is honored
    if the name fits the role; otherwise the LLM picks.

    Phase 12B.3 iteration loop: when `tweak_request` is set alongside
    `previous_bot_py` and `previous_requirements_txt`, the endpoint sends
    a follow-up turn that includes the prior generation + the tweak so
    Claude can refine rather than start over.
    """
    description: str = Field(min_length=8, max_length=4000)
    target_agents: Optional[list[str]] = None
    name_hint: Optional[str] = None
    # Iteration loop fields (12B.3). All three set together → refinement.
    tweak_request: Optional[str] = Field(default=None, max_length=4000)
    previous_bot_py: Optional[str] = Field(default=None, max_length=200_000)
    previous_requirements_txt: Optional[str] = Field(default=None, max_length=20_000)


class TeamPlanIn(BaseModel):
    """Input to `POST /workspaces/me/teams/plan` (Phase 12C.1).

    At least one of `readme_text`, `freeform_description`, or
    `github_repo` must be set. When `github_repo` is set, the endpoint
    fetches the README server-side via the workspace's GitHub
    integration. `github_branch` defaults to the integration's default
    branch (typically `main`).
    """
    readme_text: Optional[str] = Field(default=None, max_length=200_000)
    freeform_description: Optional[str] = Field(default=None, max_length=10_000)
    github_repo: Optional[str] = Field(default=None, max_length=200)
    github_branch: Optional[str] = Field(default=None, max_length=200)


class ThreadCreateIn(BaseModel):
    title: Optional[str] = None


class ThreadMessagePostIn(BaseModel):
    content: str = Field(min_length=1)


class ThreadMessageCompleteIn(BaseModel):
    content: Optional[str] = None
    error: Optional[str] = None


class ThreadMessageChunkIn(BaseModel):
    delta: str


class InstanceHeartbeatIn(BaseModel):
    instance_id: str = Field(min_length=1)
    hostname: Optional[str] = None
    pid: Optional[int] = None
    sdk_version: Optional[str] = None
    started_at: Optional[datetime] = None


# Secret names look like env vars: ASCII letter, then letters/digits/underscores.
# Cap at 64 chars so the URL stays sane and the index is small.
import re as _re
SECRET_NAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


class SecretSetIn(BaseModel):
    value: str = Field(min_length=0, max_length=8192)


class ValidatorConfigSetIn(BaseModel):
    """PUT /workspaces/me/validators/{event_kind}/{validator_name} body.

    `config` is opaque to the API — its shape is validator-specific
    (schema-strict expects `{schema: ...}`, content-rules expects
    `{rules: [...]}`) and the validator function is what defines the
    contract. We just store and forward.

    `mode` defaults to "advisory" so a Phase 7A caller that omits the
    field gets the existing behavior. "blocking" opts the validator
    into pre-emit rejection (Phase 8.2 wires that path).
    """
    config: dict[str, Any] = Field(default_factory=dict)
    mode: str = "advisory"


# Validator-name and event-kind validation: same character class as
# secret names since these strings end up in URL paths and DB columns.
VALIDATOR_NAME_RE = _re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EVENT_KIND_RE = _re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
VALIDATOR_MODES = ("advisory", "blocking")


# ---------- /workspaces/me/github (Phase 10.1) ---------- #


# GitHub repo identity validation. The GitHub username/repo regex per
# their docs allows [A-Za-z0-9._-], 1-39 chars for owner, up to 100 for
# repo. We're a touch stricter on length to match column widths and
# keep error messages tidy.
GITHUB_OWNER_RE = _re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]){0,38}$")
GITHUB_REPO_RE = _re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]){0,99}$")
GITHUB_BRANCH_RE = _re.compile(r"^[A-Za-z0-9._/\-]{1,255}$")
# Same agent-name shape used in the rest of the API.
GITHUB_AGENT_NAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


# Built once on module load so registration response builders don't
# re-do the env lookup. Same env var convention as Phase 9's
# DASHBOARD_BASE_URL.
GITHUB_WEBHOOK_BASE_URL = os.environ.get(
    "LIGHTSEI_API_BASE_URL", "https://api.lightsei.com"
).rstrip("/")


class GitHubIntegrationSetIn(BaseModel):
    """PUT /workspaces/me/github body.

    On first registration, the user provides repo + branch + PAT. The
    server generates the webhook_secret server-side (so a PAT leak
    doesn't compromise inbound-webhook authentication too) and reveals
    it once in the response. After that the secret stays encrypted at
    rest.

    Subsequent PUTs (update path: rotate the PAT, change the branch,
    move to a different repo) replace the corresponding fields.
    Webhook secret is not regenerated by default — that's a separate
    rotate operation when we want it.
    """
    repo_owner: str = Field(min_length=1, max_length=255)
    repo_name: str = Field(min_length=1, max_length=255)
    branch: str = Field(default="main", min_length=1, max_length=255)
    # PAT is plaintext on the wire and never echoed back; we encrypt
    # before storing.
    pat: str = Field(min_length=1, max_length=512)


class GitHubAgentPathSetIn(BaseModel):
    """PUT /workspaces/me/github/agents/{agent_name} body.

    `path` is repo-relative. Forward slashes for nested dirs. No
    leading slash, no `..` segments — validated below in
    _validate_github_path.
    """
    path: str = Field(min_length=1, max_length=512)


def _validate_github_path(path: str) -> None:
    """No leading slash, no `..` segments, no whitespace-only."""
    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="path must not be blank")
    if path.startswith("/"):
        raise HTTPException(
            status_code=400,
            detail="path is repo-relative; remove the leading slash",
        )
    # Block `..` anywhere it could be a path segment. Conservative —
    # we'd rather reject `foo..bar` (probably fine but uncommon) than
    # let a `../` slip through.
    if ".." in path.split("/"):
        raise HTTPException(
            status_code=400,
            detail="path may not contain `..` segments",
        )
    if "\\" in path:
        raise HTTPException(
            status_code=400,
            detail="use forward slashes for nested paths, not backslashes",
        )


def _generate_webhook_secret() -> str:
    """40-char URL-safe random string. GitHub allows arbitrary
    secret strings; we keep ours alphanumeric for paste-friendliness."""
    return _stdlib_secrets.token_urlsafe(30)  # ~40 chars after b64


def _mask_pat(plaintext: str) -> str:
    """Display-time mask. Keep first 4 + last 4 so the user can
    visually confirm "yes, this is the token I added" without seeing
    the secret bytes."""
    if len(plaintext) <= 12:
        return "***"
    return f"{plaintext[:4]}...{plaintext[-4:]}"


def _serialize_github_integration(
    g: "GitHubIntegration",
    *,
    pat_plaintext: Optional[str] = None,
    webhook_secret_plaintext: Optional[str] = None,
) -> dict[str, Any]:
    """Standard GET response shape. PAT is masked unless
    `pat_plaintext` is passed (we never pass it on GET — only on the
    initial PUT response, and even there we mask). Webhook secret
    `webhook_secret_plaintext` is included only on first creation
    so the user can paste it into GitHub; it's omitted on subsequent
    GETs to avoid leaving it lying around in browser history."""
    masked = _mask_pat(pat_plaintext) if pat_plaintext else "***"
    out: dict[str, Any] = {
        "id": g.id,
        "repo_owner": g.repo_owner,
        "repo_name": g.repo_name,
        "branch": g.branch,
        "pat_masked": masked,
        "webhook_url": f"{GITHUB_WEBHOOK_BASE_URL}/webhooks/github",
        "has_webhook_secret": True,
        "is_active": g.is_active,
        "created_at": g.created_at.isoformat(),
        "updated_at": g.updated_at.isoformat(),
    }
    if webhook_secret_plaintext is not None:
        # Revealed exactly once, on the first registration. The
        # explicit `.webhook_secret` key signals to clients that this
        # is the only chance to capture it.
        out["webhook_secret"] = webhook_secret_plaintext
        out["webhook_secret_reveal_note"] = (
            "Save this secret now — it is shown once. To rotate, "
            "DELETE the integration and re-register."
        )
    return out


def _serialize_github_agent_path(p: "GitHubAgentPath") -> dict[str, Any]:
    return {
        "agent_name": p.agent_name,
        "path": p.path,
        "created_at": p.created_at.isoformat(),
        "updated_at": p.updated_at.isoformat(),
    }


def _serialize_api_key(k: ApiKey) -> dict[str, Any]:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "created_at": k.created_at.isoformat(),
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None,
    }


def _serialize_workspace(w: Workspace) -> dict[str, Any]:
    return {
        "id": w.id,
        "name": w.name,
        "created_at": w.created_at.isoformat(),
        # Phase 11B.1: workspace-level monthly spend cap. NULL = no cap.
        "budget_usd_monthly": (
            float(w.budget_usd_monthly)
            if w.budget_usd_monthly is not None
            else None
        ),
        # Phase 17.7: billing surface so /account can render the right
        # CTA. has_stripe_customer is true iff the workspace has ever
        # been through Checkout (the portal endpoint 400s otherwise).
        "plan_tier": w.plan_tier,
        "free_credits_remaining_usd": float(w.free_credits_remaining_usd or 0),
        "has_stripe_customer": bool(w.stripe_customer_id),
    }


app = FastAPI(title="Lightsei Backend", version="0.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(BodySizeLimitMiddleware)


@app.on_event("startup")
def on_startup() -> None:
    upgrade_to_head()
    # Phase 11B.1: re-assert the model_pricing table from the
    # pricing.PRICING source-of-truth dict so a release can update
    # prices without a manual UPDATE. Idempotent.
    from pricing import seed_model_pricing
    with session_scope() as s:
        try:
            seed_model_pricing(s)
        except Exception as e:
            # Don't crash startup on a pricing-sync failure — the
            # cost computation in cost.py reads from the dict directly,
            # so worst case the dashboard's pricing-table view shows
            # stale rows for one cycle.
            import logging
            logging.getLogger("lightsei.startup").warning(
                "model_pricing seed failed: %s", e
            )

    # Phase 12C.6.2: in-process runner for /agents/generate and
    # /teams/plan. Picks pending generation_jobs rows and runs them
    # off the request path so long Anthropic calls don't race the
    # edge timeout. See backend/jobs.py.
    import jobs
    jobs.start_runner()

    # Phase 14.3: periodic eval cron. Drops one eval_runs job per
    # workspace per LIGHTSEI_EVAL_INTERVAL_S (default 3600s); the
    # runner above picks them up and the judge-LLM verdict lands on
    # run_evaluations.
    import eval_runner
    eval_runner.start_eval_cron()


@app.on_event("shutdown")
async def on_shutdown() -> None:
    import eval_runner
    import jobs
    await eval_runner.stop_eval_cron()
    await jobs.stop_runner()


# Phase 11.7 demo trigger v6: full auto-approve chain — should land green ✅ in Slack with zero clicks.
@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness + lightweight pool/connection telemetry.

    The pool counters come from SQLAlchemy's QueuePool (free, no IO). The
    pg_stat_activity probe is one short query against a system view —
    cheap enough to run on every /health hit and gives us a real
    server-side view of connection state. Both are here (rather than a
    separate /metrics) so the existing keepalive cron and Railway's
    health check graph it for free.
    """
    from db import engine as _engine

    pool = _engine.pool
    pool_stats: dict[str, Any] = {
        "size": pool.size(),
        "checked_in": pool.checkedin(),
        "checked_out": pool.checkedout(),
        "overflow": pool.overflow(),
    }

    db_stats: dict[str, Any] = {}
    try:
        with _engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT
                      COUNT(*) FILTER (WHERE state = 'idle in transaction') AS idle_in_txn,
                      COUNT(*) FILTER (WHERE state = 'active')              AS active,
                      COUNT(*) FILTER (WHERE state = 'idle')                AS idle,
                      COUNT(*)                                              AS total
                    FROM pg_stat_activity
                    WHERE datname = current_database()
                    """
                )
            ).one()
            db_stats = {
                "idle_in_txn": int(row.idle_in_txn or 0),
                "active": int(row.active or 0),
                "idle": int(row.idle or 0),
                "total": int(row.total or 0),
            }
    except Exception as e:
        db_stats = {"error": f"{type(e).__name__}: {e}"}

    return {"status": "ok", "pool": pool_stats, "db": db_stats}


def _rate_limited_workspace_id(
    auth: AuthResult = Depends(get_authenticated),
) -> str:
    """Rate-limits the /events ingest path per-credential, then returns the
    workspace_id. Per-credential (not per-workspace) so a leaked or runaway
    api_key throttles itself without taking out the dashboard's session."""
    cred_id = auth.api_key.id if auth.api_key else (
        auth.session.id if auth.session else auth.workspace_id
    )
    limits.limit_events_per_credential(cred_id)
    return auth.workspace_id


@app.post("/events")
def post_event(
    event: EventIn,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(_rate_limited_workspace_id),
) -> dict[str, Any]:
    ts = event.timestamp or utcnow()
    ensure_agent(session, workspace_id, event.agent_name, ts)

    # Phase 8.2: evaluate every registered validator pre-emit so the
    # pipeline can short-circuit on a blocking-mode FAIL before the
    # event row is created. Advisory results from the same pass get
    # written as event_validations rows after the insert succeeds.
    outcomes = evaluate_validators(
        session, workspace_id, event.kind, event.payload
    )
    blockers = find_blocking_failures(outcomes)
    if blockers:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "event rejected by blocking validator",
                "violations": [
                    {
                        "validator": o.validator_name,
                        **v,
                    }
                    for o in blockers
                    for v in o.violations
                ],
            },
        )

    run = session.get(Run, event.run_id)
    if run is None:
        run = Run(
            id=event.run_id,
            workspace_id=workspace_id,
            agent_name=event.agent_name,
            started_at=ts,
            ended_at=None,
        )
        session.add(run)
    elif run.workspace_id != workspace_id:
        # Run id collision across workspaces — refuse rather than leak.
        raise HTTPException(status_code=409, detail="run id belongs to another workspace")

    if event.kind in ("run_ended", "run_completed", "run_failed"):
        run.ended_at = ts

    # Phase 11B.1: incrementally roll up per-run cost. We need a flushed
    # run row before add_run_cost_from_event can session.get(Run, ...),
    # so flush here for the freshly-added case.
    #
    # Phase 12D follow-up: failed calls bill input tokens at almost every
    # provider (the model loaded the prompt before the failure / refusal),
    # so they're real money that produced no output. Run them through the
    # same cost path — the helper already handles output_tokens=0 cleanly.
    if event.kind in ("llm_call_completed", "llm_call_failed"):
        session.flush()
        add_run_cost_from_event(session, event.run_id, event.payload or {})

    row = Event(
        workspace_id=workspace_id,
        run_id=event.run_id,
        agent_name=event.agent_name,
        kind=event.kind,
        payload=event.payload,
        timestamp=ts,
    )
    session.add(row)
    session.flush()

    # Phase 7.3 audit trail: persist every outcome (advisory and
    # blocking-pass alike) so the dashboard's chips and the
    # /events/{id}/validations endpoint have data to render.
    write_validation_rows(session, row.id, outcomes)

    # Phase 9.4: figure out which symbolic notification triggers fired
    # and enqueue dispatch tasks. The plan-building reads channels from
    # the DB synchronously (cheap), then BackgroundTasks runs the
    # actual HTTP-out after the response is sent (slow). A misconfigured
    # channel or unreachable webhook never blocks ingestion.
    fired = notification_triggers.detect_triggers(row, outcomes)
    if fired:
        # Attach validation outcomes to the signal payload for the
        # validation.fail trigger so the formatter can render specific
        # rules — the event's own payload doesn't carry post-emit
        # validation results.
        signal_payload = dict(row.payload or {})
        if any(o.status == "fail" for o in outcomes):
            signal_payload["validations"] = [
                {
                    "validator": o.validator_name,
                    "status": o.status,
                    "violations": o.violations,
                }
                for o in outcomes
            ]
        plans = notification_triggers.build_dispatch_plans(
            session,
            event=row,
            workspace_id=workspace_id,
            fired_triggers=fired,
            dashboard_url_for=_dashboard_url_for,
            payload_for_signal=signal_payload,
        )
        for plan in plans:
            background_tasks.add_task(
                notification_triggers.dispatch_and_persist, plan,
            )

    return {"id": row.id, "status": "ok"}


@app.post("/policy/check")
def post_policy_check(
    req: PolicyCheckIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    if req.agent_name:
        ensure_agent(session, workspace_id, req.agent_name, utcnow())
    return policies.evaluate(
        session,
        workspace_id=workspace_id,
        agent_name=req.agent_name,
        action=req.action,
        payload=req.payload,
    )


@app.get("/runs")
def get_runs(
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    limit = max(1, min(limit, 500))
    rows = session.execute(
        select(Run)
        .where(Run.workspace_id == workspace_id)
        .order_by(desc(Run.started_at))
        .limit(limit)
    ).scalars().all()
    return {
        "runs": [
            {
                "id": r.id,
                "agent_name": r.agent_name,
                "started_at": r.started_at.isoformat(),
                "ended_at": r.ended_at.isoformat() if r.ended_at else None,
            }
            for r in rows
        ]
    }


@app.get("/runs/{run_id}/events")
def get_run_events(
    run_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    run = session.get(Run, run_id)
    if run is None or run.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="run not found")
    rows = session.execute(
        select(Event)
        .where(Event.workspace_id == workspace_id, Event.run_id == run_id)
        .order_by(Event.timestamp, Event.id)
    ).scalars().all()
    return {
        "run": {
            "id": run.id,
            "agent_name": run.agent_name,
            "started_at": run.started_at.isoformat(),
            "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        },
        "events": [
            {
                "id": e.id,
                "run_id": e.run_id,
                "agent_name": e.agent_name,
                "kind": e.kind,
                "payload": e.payload or {},
                "timestamp": e.timestamp.isoformat(),
            }
            for e in rows
        ],
    }


def _serialize_agent(a: Agent) -> dict[str, Any]:
    return {
        "name": a.name,
        "daily_cost_cap_usd": a.daily_cost_cap_usd,
        "system_prompt": a.system_prompt,
        # Phase 12.1: per-agent provider + model pin. null = use whatever
        # the SDK's auto-patches reported on the latest llm_call_completed.
        "provider": a.provider,
        "model": a.model,
        # Per-agent tick interval (seconds). null = use the bot's env default.
        "tick_interval_s": a.tick_interval_s,
        "description": a.description,
        # Phase 16.1: trust-zone sensitivity ladder.
        "sensitivity_level": a.sensitivity_level,
        # Phase 16.2: per-agent capability allow-list (default-deny).
        # SDK gate in 16.3 refuses ops not on this list.
        "capabilities": list(a.capabilities or []),
        # Phase 16.4: opt-in for cross-zone dispatch. False = same-zone-only
        # dispatches; True = source can target agents in different zones
        # (but auto-approval rules still apply on top).
        "dispatches_cross_zone": bool(a.dispatches_cross_zone),
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


@app.get("/agents")
def list_agents(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    # `lightsei.*` is reserved for synthetic platform agents (e.g. the
    # `lightsei.system` row that absorbs cost from server-side generator
    # calls). They have Run rows for cost accounting but aren't user bots,
    # so keep them off the agents list.
    rows = session.execute(
        select(Agent)
        .where(Agent.workspace_id == workspace_id)
        .where(~Agent.name.like("lightsei.%"))
        .order_by(Agent.name)
    ).scalars().all()
    return {"agents": [_serialize_agent(a) for a in rows]}


@app.get("/agents/{agent_name}")
def get_agent(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return _serialize_agent(a)


@app.patch("/agents/{agent_name}")
def patch_agent(
    agent_name: str,
    body: AgentPatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=500, detail="agent ensure failed")
    # Only update fields the caller actually included. None means "clear".
    fields = body.model_fields_set
    if "daily_cost_cap_usd" in fields:
        a.daily_cost_cap_usd = body.daily_cost_cap_usd
    if "system_prompt" in fields:
        # blank/whitespace -> clear; otherwise store as-is
        if body.system_prompt and body.system_prompt.strip():
            a.system_prompt = body.system_prompt
        else:
            a.system_prompt = None
    if "provider" in fields:
        if body.provider is None or not body.provider.strip():
            a.provider = None
        else:
            normalized = body.provider.strip().lower()
            if normalized not in SUPPORTED_PROVIDERS:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"unknown provider {body.provider!r}; expected one "
                        f"of {sorted(SUPPORTED_PROVIDERS)}"
                    ),
                )
            a.provider = normalized
    if "model" in fields:
        if body.model is None or not body.model.strip():
            a.model = None
        else:
            a.model = body.model.strip()
    if "tick_interval_s" in fields:
        if body.tick_interval_s is None:
            a.tick_interval_s = None
        else:
            v = int(body.tick_interval_s)
            if v < AGENT_TICK_INTERVAL_MIN_S or v > AGENT_TICK_INTERVAL_MAX_S:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"tick_interval_s must be between "
                        f"{AGENT_TICK_INTERVAL_MIN_S} and "
                        f"{AGENT_TICK_INTERVAL_MAX_S} seconds (got {v})"
                    ),
                )
            a.tick_interval_s = v
    if "description" in fields:
        if body.description is None or not body.description.strip():
            a.description = None
        else:
            a.description = body.description.strip()
    if "sensitivity_level" in fields:
        from models import is_valid_sensitivity_level
        if body.sensitivity_level is None:
            # null = no-op rather than 'reset to default'; the existing
            # value stays. Forces the user to pick a valid level
            # explicitly if they want to change it.
            pass
        elif not is_valid_sensitivity_level(body.sensitivity_level):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"sensitivity_level must be one of "
                    f"public / internal / sensitive / pii "
                    f"(got {body.sensitivity_level!r})"
                ),
            )
        else:
            a.sensitivity_level = body.sensitivity_level
    if "dispatches_cross_zone" in fields and body.dispatches_cross_zone is not None:
        a.dispatches_cross_zone = bool(body.dispatches_cross_zone)
    a.updated_at = now
    session.flush()
    return _serialize_agent(a)


# ---------- Phase 16.2: per-agent capability allow-list ---------- #


class AgentCapabilitiesPatchIn(BaseModel):
    """Body for PATCH /agents/{name}/capabilities.

    `capabilities` is the full new allow-list (replace, not patch).
    Tiny enough to send whole; avoids the add-one-remove-one merge
    semantics we'd otherwise need.
    """
    capabilities: list[str]


@app.patch("/agents/{agent_name}/capabilities")
def patch_agent_capabilities(
    agent_name: str,
    body: AgentCapabilitiesPatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 16.2: replace an agent's capability allow-list.

    Validates against `backend/capabilities.py`'s vocabulary; 422s with
    the problems list on any invalid entry so the dashboard can render
    inline errors per-line. Dedupes before persisting so two-entry
    lists with the same capability twice become one.

    No SDK enforcement yet — that's Phase 16.3 (capability gate on
    outbound ops). This endpoint just makes the storage exist so 16.3
    has something to gate against.
    """
    import capabilities as _caps

    problems = _caps.validate_capability_list(body.capabilities)
    if problems:
        raise HTTPException(
            status_code=422,
            detail={"problems": problems},
        )

    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    a.capabilities = _caps.normalize_capability_list(body.capabilities)
    a.updated_at = utcnow()
    session.flush()
    return _serialize_agent(a)


@app.delete("/agents/{agent_name}")
def delete_agent(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    """Remove an agent row.

    Historical events / runs / commands are NOT deleted — those keep
    their string `agent_name` reference and stay visible on /runs and
    /dispatch as audit trail. The `agents` row going away just means
    the bot disappears from /agents and the constellation, future
    `ensure_agent` calls won't re-create it (well, they will if a new
    bot starts emitting under the same name — that's fine).

    GitHub agent paths and any auto-approval rules referencing this
    agent are left in place; the user can clean those up separately
    if they really want a full purge.
    """
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    session.delete(a)
    session.flush()
    return {"status": "ok"}


def _serialize_plan_event(
    row: Event, validations: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "event_id": row.id,
        "run_id": row.run_id,
        "agent_name": row.agent_name,
        "timestamp": row.timestamp.isoformat(),
        "payload": row.payload or {},
    }
    if validations is not None:
        base["validations"] = validations
    return base


def _validation_summaries_for_events(
    session: Session, event_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    """Bulk-fetch validation summaries for a list of event ids.

    Returns `{event_id: [{validator, status, violation_count}]}`. Used by
    the list-plans endpoint to avoid an N+1 fetch when rendering
    sidebar chips. Each entry is the lite view: enough to render a
    PASS / FAIL / WARN chip but no actual violation details.
    """
    if not event_ids:
        return {}
    rows = session.execute(
        select(EventValidation)
        .where(EventValidation.event_id.in_(event_ids))
        .order_by(EventValidation.event_id, EventValidation.validator_name)
    ).scalars().all()
    out: dict[int, list[dict[str, Any]]] = {eid: [] for eid in event_ids}
    for v in rows:
        out[v.event_id].append({
            "validator": v.validator_name,
            "status": v.status,
            "violation_count": len(v.violations or []),
        })
    return out


def _validations_for_event(
    session: Session, event_id: int
) -> list[dict[str, Any]]:
    """Full validation rows for one event, including violation details.

    Used by `/latest-plan` (single plan, fine to be fat) and
    `/events/{id}/validations`. The list-plans endpoint uses
    `_validation_summaries_for_events` instead to keep responses lean.
    """
    rows = session.execute(
        select(EventValidation)
        .where(EventValidation.event_id == event_id)
        .order_by(EventValidation.validator_name)
    ).scalars().all()
    return [
        {
            "validator": v.validator_name,
            "status": v.status,
            "violations": v.violations or [],
        }
        for v in rows
    ]


@app.get("/agents/{agent_name}/latest-plan")
def get_agent_latest_plan(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Most recent `polaris.plan` event for the given agent, scoped
    to the calling workspace. Returns 404 when the agent has no plan
    events yet.

    Includes the full validation results for the plan: the dashboard
    selects this plan by default, so we ship the violation details
    inline to avoid a follow-up fetch on first render. The list
    endpoint trades that off for response size and only includes
    summary chips.
    """
    row = session.execute(
        select(Event)
        .where(
            Event.workspace_id == workspace_id,
            Event.agent_name == agent_name,
            Event.kind == "polaris.plan",
        )
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no plan yet")
    return _serialize_plan_event(row, _validations_for_event(session, row.id))


@app.get("/agents/{agent_name}/plans")
def list_agent_plans(
    agent_name: str,
    limit: int = 20,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Recent `polaris.plan` events for the given agent (newest
    first), capped at `limit` (1..100, default 20). Powers the
    dashboard's plan-history sidebar — returning full payloads
    means the sidebar and the detail pane share data without a
    second fetch.

    Each plan carries a `validations` array of summaries
    (`{validator, status, violation_count}`), enough to render a
    chip on each sidebar row without inflating the response with
    full violation lists. The dashboard fetches full violations via
    `/events/{event_id}/validations` when the user clicks a
    historical plan; the latest plan ships full violations on
    `/latest-plan`.

    Empty list (200) when the agent has no plan events yet —
    distinct from latest-plan's 404 because callers iterating
    history shouldn't have to special-case "agent never emitted."
    """
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=400, detail="limit must be 1..100")
    rows = session.execute(
        select(Event)
        .where(
            Event.workspace_id == workspace_id,
            Event.agent_name == agent_name,
            Event.kind == "polaris.plan",
        )
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(limit)
    ).scalars().all()
    summaries = _validation_summaries_for_events(session, [r.id for r in rows])
    return {
        "plans": [
            _serialize_plan_event(r, summaries.get(r.id, []))
            for r in rows
        ]
    }


@app.get("/agents/{agent_name}/latest-cost-analysis")
def latest_cost_analysis(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 12D.2: most recent `polaris.cost_analysis` event for the
    given agent, scoped to the calling workspace. 404 when the agent
    has not emitted one yet (which is the common case for a fresh
    workspace — the home page treats 404 as "no insights to surface").
    """
    row = session.execute(
        select(Event)
        .where(
            Event.workspace_id == workspace_id,
            Event.agent_name == agent_name,
            Event.kind == "polaris.cost_analysis",
        )
        .order_by(Event.timestamp.desc(), Event.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no cost_analysis yet")
    return {
        "event_id": row.id,
        "timestamp": row.timestamp.isoformat(),
        "payload": row.payload or {},
    }


@app.get("/events/{event_id}/validations")
def get_event_validations(
    event_id: int,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Full validation results for one event.

    Workspace-scoped: 404 if the event doesn't exist or belongs to a
    different workspace. The check is two-step (event row exists,
    event.workspace_id matches) rather than one query so we never
    leak cross-workspace event existence via timing — both branches
    return the same 404 detail.
    """
    event = session.get(Event, event_id)
    if event is None or event.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="event not found")
    return {
        "event_id": event_id,
        "validations": _validations_for_event(session, event_id),
    }


@app.post("/workspaces")
def create_workspace(
    body: WorkspaceCreateIn, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Public signup. Creates a workspace plus its first API key, and returns
    the plaintext key once. The plaintext is never retrievable again.
    """
    now = utcnow()
    ws = Workspace(id=str(uuid.uuid4()), name=body.name, created_at=now)
    session.add(ws)
    session.flush()
    seed_default_validators(session, ws.id, now)

    plaintext = generate_key()
    api_key_row = ApiKey(
        id=str(uuid.uuid4()),
        workspace_id=ws.id,
        name=body.api_key_name,
        prefix=prefix_for_display(plaintext),
        hash=hash_token(plaintext),
        created_at=now,
    )
    session.add(api_key_row)
    session.flush()
    return {
        "workspace": _serialize_workspace(ws),
        "api_key": _serialize_api_key(api_key_row) | {"plaintext": plaintext},
    }


@app.get("/workspaces/me")
def get_me(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    return _serialize_workspace(ws)


@app.get("/workspaces/me/cost")
def get_workspace_cost(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 11B.1: month-to-date workspace spend + projected EOM +
    per-agent and per-model breakdown + budget bar state.

    Reads `runs.cost_usd` directly for the rollup so the dashboard can
    poll this every 30s without re-summing events × pricing per call.
    """
    return workspace_cost_mtd(session, workspace_id)


@app.get("/workspaces/me/zone-presets")
def get_zone_presets(
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 16.7: list the three trust-zone presets the team-from-README
    picker offers.

    Workspace-authed but workspace-independent — the presets are
    global. The auth check is here so an unauth caller can't enumerate
    the workspace's UI surfaces, but the response body doesn't depend
    on which workspace is asking.

    Each entry carries the dashboard-renderable metadata (label,
    summary, tradeoff) + the full `by_role` map so the picker can
    show a preview of what each role gets without a follow-up fetch.
    """
    import zone_presets as _zp
    return {"presets": _zp.list_presets()}


@app.get("/workspaces/me/cost/insights")
def get_cost_insights(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 12D.1: where your dollars went, what was wasted, and
    one-click fixes where they exist.

    Returns a homogeneous list of insight dicts (model-tier swaps that
    would save money, agents with low useful-rate, cache savings to
    date, failed-call cost, plan volatility for tick-interval tuning).
    Pure analytics over existing data — no LLM calls, no schema
    changes; safe to poll on every page load (the /cost/insights page
    refreshes every 30s the same way /cost does).
    """
    import cost_insights as _ci
    return _ci.all_insights(session, workspace_id)


@app.get("/workspaces/me/quality")
def get_workspace_quality(
    days: int = 7,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 14.4: workspace-wide quality rollup.

    Pure read over `run_evaluations` (filled by Phase 14.3's eval
    runner). Per-agent verdict counts + trend + workspace-wide
    aggregate + the top recent-bads list. Safe to poll on every
    /agents page load.
    """
    import quality_signal
    return quality_signal.workspace_quality(session, workspace_id, days=days)


@app.get("/workspaces/me/agents/{agent_name}/quality")
def get_agent_quality(
    agent_name: str,
    days: int = 7,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 14.4: per-agent quality summary.

    Powers /agents/{name}'s Quality section: verdict_counts,
    total_evaluations, trend vs the prior window of the same length,
    and the most-recent bad evaluations with their judge reasons so
    the user can see _why_ a bot is flagged without an extra fetch.
    """
    import quality_signal
    return quality_signal.agent_quality(
        session, workspace_id, agent_name, days=days,
    )


@app.get("/workspaces/me/pulse")
def get_workspace_pulse(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 11B.2: drives the home-page status hero. Returns a small
    counts-and-times payload the hero can render in one read; polled
    every 5s so the hero copy stays current without a websocket.

    `status` is 'calm' when issues_count is 0, 'attention' otherwise.
    `issues` breaks down what wants the operator's eyes:
      pending_approvals  — Phase 11.2 commands stuck waiting on a click
      failed_validations — Phase 8 blocking-validator denials in the
                           last 24h that haven't been re-emitted clean
      budget_warnings    — workspace MTD ≥ 80% of budget_usd_monthly
                           (1 if true, 0 if cap unset or under threshold)
      stale_agents       — agents with heartbeats older than 5 min
                           (bots that died without an explicit stop)
    `last_event_at` powers the hero's pulsing constellation icon —
    when this value moves, the icon brightens for one polling cycle.
    """
    now = utcnow()
    cutoff_24h = now - timedelta(hours=24)
    cutoff_5m = now - timedelta(minutes=5)

    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="workspace not found")

    pending_approvals = session.execute(
        text(
            """
            SELECT COUNT(*) AS n FROM commands
            WHERE workspace_id = :wsid
              AND approval_state = 'pending'
              AND expires_at > :now
            """
        ),
        {"wsid": workspace_id, "now": now},
    ).scalar_one()

    # Failed validations in the last 24h, EXCEPT ones whose validator
    # has been edited since the failure landed. Editing a rule on
    # /validators is the user's signal that they've addressed it; old
    # fails of that rule are stale (the new pattern may not even fire
    # on that input anymore) and shouldn't keep nagging the home pulse.
    failed_validations = session.execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM event_validations ev
            JOIN events e ON e.id = ev.event_id
            LEFT JOIN validator_configs vc
              ON vc.workspace_id = e.workspace_id
              AND vc.event_kind = e.kind
              AND vc.validator_name = ev.validator_name
            WHERE e.workspace_id = :wsid
              AND ev.status = 'fail'
              AND ev.created_at >= :cutoff_24h
              AND (vc.updated_at IS NULL OR ev.created_at >= vc.updated_at)
            """
        ),
        {"wsid": workspace_id, "cutoff_24h": cutoff_24h},
    ).scalar_one()

    # Budget warning: only meaningful when a cap is set. 80% threshold
    # matches the "warns at 80%, denies at 100%" copy in the Phase
    # 11B.1 cost panel.
    budget_warnings = 0
    if workspace.budget_usd_monthly is not None and float(
        workspace.budget_usd_monthly
    ) > 0:
        cost = workspace_cost_mtd(session, workspace_id)
        used_pct = cost.get("budget_used_pct") or 0.0
        if used_pct >= 80.0:
            budget_warnings = 1

    # Stale = recently-relevant agent that stopped heartbeating. The
    # earlier definition ("any agent that ever had a heartbeat but
    # hasn't beat in 5 min") flagged abandoned test bots from weeks
    # ago and quietly inflated the attention counter forever. Tighten:
    # require some signal of recent relevance (heartbeat or event in
    # the last 24h). An agent that was running last week and isn't now
    # is just stopped, not stale — drop it from the count.
    cutoff_24h_stale = now - timedelta(hours=24)
    stale_agents = session.execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM (
                SELECT
                    a.name,
                    (SELECT MAX(ai.last_heartbeat_at)
                     FROM agent_instances ai
                     WHERE ai.workspace_id = :wsid
                       AND ai.agent_name = a.name) AS last_hb,
                    (SELECT MAX(e.timestamp)
                     FROM events e
                     WHERE e.workspace_id = :wsid
                       AND e.agent_name = a.name) AS last_event
                FROM agents a
                WHERE a.workspace_id = :wsid
            ) sub
            WHERE sub.last_hb IS NOT NULL
              AND sub.last_hb < :cutoff_5m
              AND (
                  sub.last_hb >= :cutoff_24h
                  OR (sub.last_event IS NOT NULL AND sub.last_event >= :cutoff_24h)
              )
            """
        ),
        {
            "wsid": workspace_id,
            "cutoff_5m": cutoff_5m,
            "cutoff_24h": cutoff_24h_stale,
        },
    ).scalar_one()

    issues_count = (
        int(pending_approvals or 0)
        + int(failed_validations or 0)
        + int(budget_warnings or 0)
        + int(stale_agents or 0)
    )

    agent_count = session.execute(
        select(func.count()).select_from(Agent).where(
            Agent.workspace_id == workspace_id
        )
    ).scalar_one()

    last_polaris_tick = session.execute(
        text(
            """
            SELECT MAX(timestamp) FROM events
            WHERE workspace_id = :wsid
              AND agent_name = 'polaris'
              AND kind = 'polaris.plan'
            """
        ),
        {"wsid": workspace_id},
    ).scalar_one()
    last_event = session.execute(
        text(
            """
            SELECT MAX(timestamp) FROM events
            WHERE workspace_id = :wsid
            """
        ),
        {"wsid": workspace_id},
    ).scalar_one()

    return {
        "status": "calm" if issues_count == 0 else "attention",
        "issues_count": issues_count,
        "issues": {
            "pending_approvals": int(pending_approvals or 0),
            "failed_validations": int(failed_validations or 0),
            "budget_warnings": int(budget_warnings or 0),
            "stale_agents": int(stale_agents or 0),
        },
        "workspace_name": workspace.name,
        "agent_count": int(agent_count or 0),
        "last_polaris_tick_at": (
            last_polaris_tick.isoformat() if last_polaris_tick else None
        ),
        "last_event_at": (
            last_event.isoformat() if last_event else None
        ),
        "as_of": now.isoformat(),
    }


@app.get("/workspaces/me/constellation")
def get_workspace_constellation(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 11B.3: nodes + edges that drive the home-page constellation map.

    Returns:
      agents: one row per agent with role + status + 24h-window activity
              counters (runs, cost, dispatches in/out) + model + last-event
              timestamp. The home-page hero pulls its "last activity"
              animation trigger from the max(last_event_at) across this list.
      edges:  one row per (source, target) agent pair with at least one
              dispatched command in the last 24h. count_24h drives stroke
              opacity; last_at drives the brief edge-pulse animation when
              a fresh dispatch lands within the polling window.

    Polled by the dashboard every 5s (and paused when the tab is hidden).
    Filters out agents with no activity AND no agent_instances row — keeps
    "dead" agents that were never deployed off the canvas.
    """
    cutoff_24h = utcnow() - timedelta(hours=24)

    # Per-agent rollup. Single LEFT JOIN against agent_instances for
    # last-heartbeat-derived status, and aggregates against runs +
    # commands for the 24h activity counters.
    agent_rows = session.execute(
        text(
            """
            SELECT
                a.name,
                a.role,
                a.system_prompt,
                a.sensitivity_level,
                COALESCE(
                    (
                        SELECT MAX(ai.last_heartbeat_at)
                        FROM agent_instances ai
                        WHERE ai.workspace_id = :wsid
                          AND ai.agent_name = a.name
                    ),
                    NULL
                ) AS last_heartbeat_at,
                COALESCE(
                    (
                        SELECT COUNT(*) FROM runs r
                        WHERE r.workspace_id = :wsid
                          AND r.agent_name = a.name
                          AND r.started_at >= :cutoff_24h
                    ),
                    0
                ) AS runs_24h,
                COALESCE(
                    (
                        SELECT SUM(r.cost_usd) FROM runs r
                        WHERE r.workspace_id = :wsid
                          AND r.agent_name = a.name
                          AND r.started_at >= :cutoff_24h
                    ),
                    0
                ) AS cost_24h_usd,
                (
                    SELECT MAX(e.timestamp) FROM events e
                    WHERE e.workspace_id = :wsid
                      AND e.agent_name = a.name
                ) AS last_event_at,
                (
                    SELECT MAX(e.payload->>'model') FROM events e
                    WHERE e.workspace_id = :wsid
                      AND e.agent_name = a.name
                      AND e.kind = 'llm_call_completed'
                      AND e.timestamp >= :cutoff_24h
                ) AS recent_model
            FROM agents a
            WHERE a.workspace_id = :wsid
              AND a.name NOT LIKE 'lightsei.%'
            ORDER BY a.name
            """
        ),
        {"wsid": workspace_id, "cutoff_24h": cutoff_24h},
    ).all()

    # Active-only filter so old test bots / dev agents don't crowd the
    # canvas. Keep an agent if any of:
    #   - role == "orchestrator" (polaris always renders, anchors center)
    #   - heartbeat within the last hour (currently running on a worker)
    #   - any event in the last 24h (recently active even without a heartbeat)
    # Anything else is treated as dormant and dropped from the response.
    now = utcnow()
    cutoff_1h = now - timedelta(hours=1)
    agents_out: list[dict[str, Any]] = []
    for r in agent_rows:
        last_hb = r.last_heartbeat_at
        last_event = r.last_event_at
        is_orchestrator = r.role == "orchestrator"
        recently_alive = last_hb is not None and last_hb >= cutoff_1h
        recently_active = last_event is not None and last_event >= cutoff_24h
        if not (is_orchestrator or recently_alive or recently_active):
            continue
        # Status mapping:
        #   active = heartbeat within last 60s
        #   stale  = heartbeat seen but > 60s ago
        #   stopped = no heartbeat ever (but had runs_24h, so they ran
        #            via a worker that didn't register an instance row)
        if last_hb is None:
            status = "stopped"
        elif (now - last_hb).total_seconds() <= 60:
            status = "active"
        else:
            status = "stale"
        agents_out.append({
            "name": r.name,
            "role": r.role,
            "model": r.recent_model,
            "status": status,
            "runs_24h": int(r.runs_24h or 0),
            "cost_24h_usd": float(round(r.cost_24h_usd or 0, 6)),
            "last_event_at": (
                r.last_event_at.isoformat() if r.last_event_at else None
            ),
            "last_heartbeat_at": (
                last_hb.isoformat() if last_hb else None
            ),
            # Phase 16.6: drives node coloring on the constellation map.
            "sensitivity_level": r.sensitivity_level or "internal",
        })

    # Dispatch edges: pairs of (source, target) with at least one
    # dispatched command in the last 24h. Phase 11.2 lit this up by
    # adding the source_agent column on commands; the constellation
    # map's bezier edges read straight from this query.
    edge_rows = session.execute(
        text(
            """
            SELECT
                source_agent AS src,
                agent_name   AS tgt,
                COUNT(*)     AS count_24h,
                MAX(created_at) AS last_at
            FROM commands
            WHERE workspace_id = :wsid
              AND source_agent IS NOT NULL
              AND created_at >= :cutoff_24h
            GROUP BY source_agent, agent_name
            ORDER BY count_24h DESC
            """
        ),
        {"wsid": workspace_id, "cutoff_24h": cutoff_24h},
    ).all()

    # Filter edges to only those whose endpoints are both in the
    # rendered agent list — keeps the canvas free of dangling edges
    # to filtered-out dormant agents.
    rendered_names = {a["name"] for a in agents_out}
    edges_out = [
        {
            "from": e.src,
            "to": e.tgt,
            "count_24h": int(e.count_24h or 0),
            "last_at": e.last_at.isoformat() if e.last_at else None,
        }
        for e in edge_rows
        if e.src in rendered_names and e.tgt in rendered_names
    ]

    return {"agents": agents_out, "edges": edges_out}


@app.patch("/workspaces/me")
def patch_me(
    body: WorkspacePatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    fields = body.model_fields_set
    if "name" in fields and body.name is not None:
        ws.name = body.name
    if "budget_usd_monthly" in fields:
        # Phase 11B.1: explicit null clears the cap. A patch that omits the
        # field entirely leaves it untouched (model_fields_set captures the
        # difference).
        if body.budget_usd_monthly is None:
            ws.budget_usd_monthly = None
        else:
            if body.budget_usd_monthly < 0:
                raise HTTPException(
                    status_code=400,
                    detail="budget_usd_monthly must be >= 0",
                )
            ws.budget_usd_monthly = Decimal(format(body.budget_usd_monthly, ".2f"))
    session.flush()
    return _serialize_workspace(ws)


@app.get("/workspaces/me/api-keys")
def list_my_keys(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(ApiKey).where(ApiKey.workspace_id == workspace_id).order_by(ApiKey.created_at)
    ).scalars().all()
    return {"api_keys": [_serialize_api_key(k) for k in rows]}


@app.post("/workspaces/me/api-keys")
def create_my_key(
    body: ApiKeyCreateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    plaintext = generate_key()
    row = ApiKey(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        name=body.name,
        prefix=prefix_for_display(plaintext),
        hash=hash_token(plaintext),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return _serialize_api_key(row) | {"plaintext": plaintext}


@app.delete("/workspaces/me/api-keys/{key_id}")
def revoke_my_key(
    key_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    row = session.get(ApiKey, key_id)
    if row is None or row.workspace_id != auth.workspace_id:
        raise HTTPException(status_code=404, detail="api key not found")
    if auth.api_key is not None and row.id == auth.api_key.id:
        raise HTTPException(
            status_code=400, detail="cannot revoke the key used for this request"
        )
    if row.revoked_at is None:
        row.revoked_at = utcnow()
    session.flush()
    return _serialize_api_key(row)


# ---------- /workspaces/me/secrets ---------- #

def _serialize_secret_meta(s: WorkspaceSecret) -> dict[str, Any]:
    """Metadata only — never exposes the encrypted blob or decrypted value."""
    return {
        "name": s.name,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _validate_secret_name(name: str) -> None:
    if not SECRET_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "secret name must start with a letter and contain only letters, "
                "digits, or underscores (max 64 chars)"
            ),
        )


def _require_secrets_available() -> None:
    if not secrets_crypto.is_available():
        raise HTTPException(
            status_code=503,
            detail="secrets store unavailable: LIGHTSEI_SECRETS_KEY is not configured",
        )


@app.get("/workspaces/me/secrets")
def list_secrets(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Names + timestamps only. Values are never included in this endpoint —
    fetch each one individually via GET /workspaces/me/secrets/{name}."""
    rows = session.execute(
        select(WorkspaceSecret)
        .where(WorkspaceSecret.workspace_id == workspace_id)
        .order_by(WorkspaceSecret.name)
    ).scalars().all()
    return {"secrets": [_serialize_secret_meta(s) for s in rows]}


@app.get("/workspaces/me/secrets/{name}")
def get_secret(
    name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Returns the decrypted value. Anyone holding a workspace credential can
    read every secret in that workspace, by design — secrets are *not*
    a per-user permission boundary."""
    _validate_secret_name(name)
    _require_secrets_available()
    row = session.get(WorkspaceSecret, (workspace_id, name))
    if row is None:
        raise HTTPException(status_code=404, detail="secret not found")
    try:
        value = secrets_crypto.decrypt(row.encrypted_value)
    except Exception as e:
        # An encrypted blob that can't be decrypted is almost always the master
        # key changing without a rotation. Surface that clearly rather than
        # returning gibberish.
        raise HTTPException(
            status_code=500,
            detail=f"failed to decrypt secret (master key may have changed): {e}",
        )
    return {"name": row.name, "value": value}


@app.put("/workspaces/me/secrets/{name}")
def put_secret(
    name: str,
    body: SecretSetIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Idempotent create/update. The value is encrypted before being written
    to the row — plaintext never reaches disk."""
    _validate_secret_name(name)
    _require_secrets_available()
    now = utcnow()
    blob = secrets_crypto.encrypt(body.value)
    row = session.get(WorkspaceSecret, (workspace_id, name))
    if row is None:
        row = WorkspaceSecret(
            workspace_id=workspace_id,
            name=name,
            encrypted_value=blob,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.encrypted_value = blob
        row.updated_at = now
    session.flush()
    return _serialize_secret_meta(row)


@app.delete("/workspaces/me/secrets/{name}")
def delete_secret(
    name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    _validate_secret_name(name)
    row = session.get(WorkspaceSecret, (workspace_id, name))
    if row is None:
        raise HTTPException(status_code=404, detail="secret not found")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


# ---------- /workspaces/me/validators (Phase 7.3) ---------- #


def _validate_validator_path(event_kind: str, validator_name: str) -> None:
    if not EVENT_KIND_RE.match(event_kind):
        raise HTTPException(
            status_code=400,
            detail=(
                "event_kind must match [a-z][a-z0-9_.]{0,63} (lowercase, "
                "may contain dots and underscores)"
            ),
        )
    if not VALIDATOR_NAME_RE.match(validator_name):
        raise HTTPException(
            status_code=400,
            detail=(
                "validator_name must match [a-z][a-z0-9_]{0,63} (lowercase, "
                "alphanumeric + underscore)"
            ),
        )
    if validator_name not in validators.REGISTRY:
        raise HTTPException(
            status_code=400,
            detail=(
                f"validator {validator_name!r} is not in the registry; "
                f"known: {sorted(validators.REGISTRY)}"
            ),
        )


def _serialize_validator_config(c: ValidatorConfig) -> dict[str, Any]:
    return {
        "event_kind": c.event_kind,
        "validator_name": c.validator_name,
        "config": c.config,
        "mode": c.mode,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


@app.get("/workspaces/me/validators")
def list_validators(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(ValidatorConfig)
        .where(ValidatorConfig.workspace_id == workspace_id)
        .order_by(ValidatorConfig.event_kind, ValidatorConfig.validator_name)
    ).scalars().all()
    return {"validators": [_serialize_validator_config(c) for c in rows]}


@app.put("/workspaces/me/validators/{event_kind}/{validator_name}")
def put_validator(
    event_kind: str,
    validator_name: str,
    body: ValidatorConfigSetIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    _validate_validator_path(event_kind, validator_name)
    if body.mode not in VALIDATOR_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"mode must be one of {list(VALIDATOR_MODES)}; got "
                f"{body.mode!r}"
            ),
        )
    now = utcnow()
    existing = session.get(
        ValidatorConfig, (workspace_id, event_kind, validator_name)
    )
    if existing is None:
        row = ValidatorConfig(
            workspace_id=workspace_id,
            event_kind=event_kind,
            validator_name=validator_name,
            config=body.config,
            mode=body.mode,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        existing.config = body.config
        existing.mode = body.mode
        existing.updated_at = now
        row = existing
    session.flush()
    return _serialize_validator_config(row)


@app.delete("/workspaces/me/validators/{event_kind}/{validator_name}")
def delete_validator(
    event_kind: str,
    validator_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    # Path-format validation only — we don't require the validator to be
    # in the registry to delete its row. If a registry entry was renamed
    # in code, the old config row is now orphaned and the operator needs
    # to be able to clean it up.
    if not EVENT_KIND_RE.match(event_kind) or not VALIDATOR_NAME_RE.match(validator_name):
        raise HTTPException(status_code=400, detail="malformed event_kind or validator_name")
    row = session.get(
        ValidatorConfig, (workspace_id, event_kind, validator_name)
    )
    if row is None:
        raise HTTPException(status_code=404, detail="validator config not found")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


# Dashboard base URL the formatters bake into "View ↗" links inside
# notification messages. Configurable so a self-host can point at its
# own dashboard. Default matches prod.
DASHBOARD_BASE_URL = os.environ.get(
    "LIGHTSEI_DASHBOARD_BASE_URL", "https://app.lightsei.com"
).rstrip("/")


def _dashboard_url_for(trigger: str, agent_name: str, run_id: Optional[str] = None) -> str:
    """Build the deep link a notification's "View" action points at.

    polaris.plan / validation.fail send users to the Polaris page;
    run_failed sends them to the run detail. The dashboard handles
    auth + workspace selection, so we don't pass a workspace id here.
    """
    if trigger == "run_failed" and run_id:
        return f"{DASHBOARD_BASE_URL}/runs/{run_id}"
    if agent_name == "polaris" or trigger in ("polaris.plan", "validation.fail"):
        return f"{DASHBOARD_BASE_URL}/polaris"
    return f"{DASHBOARD_BASE_URL}/agents/{agent_name}"


# ---------- /workspaces/me/notifications (Phase 9.1) ---------- #
#
# Channels are workspace-scoped, unique-by-name, with a list of symbolic
# triggers (`polaris.plan`, `validation.fail`, `run_failed`). The actual
# dispatcher (HTTP-out + per-platform formatter) lands in 9.2; the
# `/test` endpoint here writes a 'skipped' delivery row in the meantime
# so the API surface is settled before 9.2 fills it in.

NOTIFICATION_CHANNEL_TYPES = ("slack", "discord", "teams", "mattermost", "webhook")
NOTIFICATION_TRIGGERS = ("polaris.plan", "validation.fail", "run_failed")
NOTIFICATION_NAME_RE = _re.compile(r"^[A-Za-z][A-Za-z0-9_\- ]{0,63}$")


class NotificationChannelCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: str
    target_url: str = Field(min_length=1, max_length=2048)
    triggers: list[str] = Field(default_factory=list)
    secret_token: Optional[str] = Field(default=None, max_length=512)
    is_active: bool = True


class NotificationChannelPatchIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    target_url: Optional[str] = Field(default=None, min_length=1, max_length=2048)
    triggers: Optional[list[str]] = None
    secret_token: Optional[str] = Field(default=None, max_length=512)
    is_active: Optional[bool] = None


def _validate_channel_input(name: str, type_: str, triggers: list[str]) -> None:
    if not NOTIFICATION_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                "name must match [A-Za-z][A-Za-z0-9_\\- ]{0,63} (start with "
                "a letter; alphanumeric, underscore, hyphen, space)"
            ),
        )
    if type_ not in NOTIFICATION_CHANNEL_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"type must be one of {list(NOTIFICATION_CHANNEL_TYPES)}; "
                f"got {type_!r}"
            ),
        )
    bad_triggers = [t for t in triggers if t not in NOTIFICATION_TRIGGERS]
    if bad_triggers:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown triggers {bad_triggers}; allowed values are "
                f"{list(NOTIFICATION_TRIGGERS)}"
            ),
        )


def _mask_url(url: str) -> str:
    """Mask a webhook URL for display.

    Keeps scheme + host so the user can recognize the platform; truncates
    the path so the secret token (which lives in the path for Slack/
    Discord/Teams/Mattermost) is never echoed back. Last 4 chars of the
    path are kept as a "yes, this is the URL I added" identity hint.
    """
    from urllib.parse import urlparse
    try:
        p = urlparse(url)
    except Exception:
        return "***"
    if not p.scheme or not p.netloc:
        return "***"
    path = p.path or ""
    if len(path) > 8:
        masked_path = f"{path[:4]}...{path[-4:]}"
    elif path:
        masked_path = "/***"
    else:
        masked_path = ""
    return f"{p.scheme}://{p.netloc}{masked_path}"


def _serialize_notification_channel(c: NotificationChannel) -> dict[str, Any]:
    return {
        "id": c.id,
        "name": c.name,
        "type": c.type,
        "target_url_masked": _mask_url(c.target_url),
        "triggers": list(c.triggers or []),
        "has_secret_token": c.secret_token is not None,
        "is_active": c.is_active,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


def _serialize_notification_delivery(d: NotificationDelivery) -> dict[str, Any]:
    return {
        "id": d.id,
        "channel_id": d.channel_id,
        "event_id": d.event_id,
        "trigger": d.trigger,
        "status": d.status,
        "response_summary": d.response_summary or {},
        "attempt_count": d.attempt_count,
        "sent_at": d.sent_at.isoformat(),
    }


@app.get("/workspaces/me/notifications")
def list_notification_channels(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(NotificationChannel)
        .where(NotificationChannel.workspace_id == workspace_id)
        .order_by(NotificationChannel.created_at)
    ).scalars().all()
    return {"channels": [_serialize_notification_channel(c) for c in rows]}


@app.post("/workspaces/me/notifications")
def create_notification_channel(
    body: NotificationChannelCreateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    _validate_channel_input(body.name, body.type, body.triggers)
    # Conflict check is a separate query rather than relying on the
    # UNIQUE constraint exception so the error message is cleaner and
    # we don't pollute the SQL session with a rolled-back insert.
    existing = session.execute(
        select(NotificationChannel)
        .where(
            NotificationChannel.workspace_id == workspace_id,
            NotificationChannel.name == body.name,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"a notification channel named {body.name!r} already exists",
        )
    now = utcnow()
    row = NotificationChannel(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        name=body.name,
        type=body.type,
        target_url=body.target_url,
        triggers=body.triggers,
        secret_token=body.secret_token,
        is_active=body.is_active,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return _serialize_notification_channel(row)


@app.get("/workspaces/me/notifications/{channel_id}")
def get_notification_channel(
    channel_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    return _serialize_notification_channel(row)


@app.patch("/workspaces/me/notifications/{channel_id}")
def patch_notification_channel(
    channel_id: str,
    body: NotificationChannelPatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    fields = body.model_fields_set
    # Re-validate any changed fields. Type isn't patchable here — to
    # change platform, delete and recreate. Keeps the upsert path tight.
    if "name" in fields and body.name is not None:
        if not NOTIFICATION_NAME_RE.match(body.name):
            raise HTTPException(status_code=400, detail="malformed name")
        if body.name != row.name:
            conflict = session.execute(
                select(NotificationChannel)
                .where(
                    NotificationChannel.workspace_id == workspace_id,
                    NotificationChannel.name == body.name,
                )
            ).scalar_one_or_none()
            if conflict is not None:
                raise HTTPException(
                    status_code=409,
                    detail=f"channel name {body.name!r} already exists",
                )
            row.name = body.name
    if "triggers" in fields and body.triggers is not None:
        bad = [t for t in body.triggers if t not in NOTIFICATION_TRIGGERS]
        if bad:
            raise HTTPException(
                status_code=400,
                detail=f"unknown triggers {bad}",
            )
        row.triggers = body.triggers
    if "target_url" in fields and body.target_url is not None:
        row.target_url = body.target_url
    if "secret_token" in fields:
        # Explicit None means "clear"; missing means "leave alone".
        row.secret_token = body.secret_token
    if "is_active" in fields and body.is_active is not None:
        row.is_active = body.is_active
    row.updated_at = utcnow()
    session.flush()
    return _serialize_notification_channel(row)


@app.delete("/workspaces/me/notifications/{channel_id}")
def delete_notification_channel(
    channel_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


@app.post("/workspaces/me/notifications/dispatch")
def dispatch_to_channel(
    body: NotificationDispatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 11.4: dispatch arbitrary text to a notification channel
    by name. Hermes (the workspace's notifier bot) calls this as the
    last hop in chains like polaris → atlas → hermes — Atlas hands
    Hermes a tidy summary line, Hermes hands it to Slack/Discord/etc.

    Returns 200 with the delivery row regardless of dispatch outcome.
    The HTTP status of the underlying webhook lives in
    `delivery.response_summary.http_status` so the bot can decide
    whether to retry or surface failure as a `hermes.send_failed`
    event.
    """
    channel = session.execute(
        select(NotificationChannel).where(
            NotificationChannel.workspace_id == workspace_id,
            NotificationChannel.name == body.channel_name,
        )
    ).scalar_one_or_none()
    if channel is None:
        raise HTTPException(
            status_code=404,
            detail=f"channel {body.channel_name!r} not found",
        )

    signal = notifications.Signal(
        trigger="hermes.post",
        agent_name="hermes",
        dashboard_url=f"{DASHBOARD_BASE_URL}/notifications",
        timestamp=utcnow(),
        payload={"text": body.text, "severity": body.severity},
        workspace_id=workspace_id,
    )
    result = notifications.dispatch(
        channel_type=channel.type,
        target_url=channel.target_url,
        signal=signal,
        secret_token=channel.secret_token,
    )

    delivery = NotificationDelivery(
        channel_id=channel.id,
        event_id=None,
        trigger="hermes.post",
        status=result.status,
        response_summary=result.response_summary,
        attempt_count=result.attempt_count,
        sent_at=utcnow(),
    )
    session.add(delivery)
    session.flush()
    return {"delivery": _serialize_notification_delivery(delivery)}


@app.post("/workspaces/me/notifications/{channel_id}/test")
def test_notification_channel(
    channel_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Fire a synthetic test message at this channel.

    Phase 9.2 swap: now does a real dispatch via the per-platform
    formatter + HTTP-out, records the result in `notification_
    deliveries`. Endpoint shape unchanged from Phase 9.1 so the
    dashboard's "send test" button doesn't need updating.

    Returns 200 with the delivery row regardless of dispatch outcome
    (sent vs failed). The HTTP status of the underlying webhook lives
    in `delivery.response_summary.http_status`. We deliberately don't
    surface failures as 5xx — a misconfigured channel is the user's
    problem to fix, not a server error to alert on.
    """
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")

    signal = notifications.Signal(
        trigger="test",
        agent_name=row.name,  # the test message references the channel by name
        dashboard_url=f"{DASHBOARD_BASE_URL}/account",
        timestamp=utcnow(),
        payload={},
        workspace_id=workspace_id,
    )
    result = notifications.dispatch(
        channel_type=row.type,
        target_url=row.target_url,
        signal=signal,
        secret_token=row.secret_token,
    )

    delivery = NotificationDelivery(
        channel_id=row.id,
        event_id=None,
        trigger="test",
        status=result.status,
        response_summary=result.response_summary,
        attempt_count=result.attempt_count,
        sent_at=utcnow(),
    )
    session.add(delivery)
    session.flush()
    return {"delivery": _serialize_notification_delivery(delivery)}


@app.get("/workspaces/me/notifications/{channel_id}/deliveries")
def list_notification_deliveries(
    channel_id: str,
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit must be 1..200")
    row = session.get(NotificationChannel, channel_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="channel not found")
    rows = session.execute(
        select(NotificationDelivery)
        .where(NotificationDelivery.channel_id == channel_id)
        .order_by(NotificationDelivery.sent_at.desc(), NotificationDelivery.id.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "deliveries": [_serialize_notification_delivery(d) for d in rows]
    }


# ---------- /workspaces/me/github (Phase 10.1) ---------- #
#
# CRUD for the workspace's GitHub integration + the per-agent path
# mappings the webhook receiver (10.2) and the redeploy pipeline (10.3)
# read from. Helpers, regexes, and Pydantic models for these endpoints
# live in the input-models section earlier in the file.


@app.put("/workspaces/me/github")
def put_github_integration(
    body: GitHubIntegrationSetIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Register or update the workspace's GitHub integration.

    On first registration: validates the PAT against the GitHub API
    (so wrong tokens fail here, not at first webhook), generates a
    fresh webhook secret, encrypts both, returns the secret in
    plaintext exactly once.

    On update: re-validates the PAT (in case the user is rotating to
    a new token) but keeps the existing webhook secret so the user's
    GitHub-side webhook config keeps working without a re-paste.
    """
    if not GITHUB_OWNER_RE.match(body.repo_owner):
        raise HTTPException(status_code=400, detail="repo_owner has an invalid GitHub username shape")
    if not GITHUB_REPO_RE.match(body.repo_name):
        raise HTTPException(status_code=400, detail="repo_name has an invalid GitHub repo shape")
    if not GITHUB_BRANCH_RE.match(body.branch):
        raise HTTPException(status_code=400, detail="branch has an invalid format")
    if not secrets_crypto.is_available():
        raise HTTPException(
            status_code=503,
            detail="secrets store unavailable: LIGHTSEI_SECRETS_KEY is not configured",
        )

    # Validate the PAT against GitHub. This is a network call but we
    # want it to be authoritative — a bad PAT registered here would
    # make every subsequent webhook + Polaris-fetch fail mysteriously.
    try:
        meta = github_api.validate_pat(
            repo_owner=body.repo_owner,
            repo_name=body.repo_name,
            pat=body.pat,
        )
    except github_api.GitHubAPIError as exc:
        if exc.kind == "transport":
            raise HTTPException(status_code=502, detail=exc.message) from exc
        raise HTTPException(status_code=400, detail=exc.message) from exc

    now = utcnow()
    existing = session.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.workspace_id == workspace_id
        )
    ).scalar_one_or_none()

    encrypted_pat = secrets_crypto.encrypt(body.pat)
    if existing is None:
        webhook_secret = _generate_webhook_secret()
        encrypted_webhook_secret = secrets_crypto.encrypt(webhook_secret)
        row = GitHubIntegration(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            repo_owner=body.repo_owner,
            repo_name=body.repo_name,
            branch=body.branch,
            encrypted_pat=encrypted_pat,
            encrypted_webhook_secret=encrypted_webhook_secret,
            is_active=True,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return {
            **_serialize_github_integration(
                row,
                pat_plaintext=body.pat,
                webhook_secret_plaintext=webhook_secret,
            ),
            "default_branch_from_github": meta.default_branch,
        }

    # Update path: rotate PAT, possibly change repo + branch, keep
    # existing webhook secret (so the user's GitHub-side config keeps
    # working without a re-paste). To rotate the secret, DELETE +
    # re-PUT.
    existing.repo_owner = body.repo_owner
    existing.repo_name = body.repo_name
    existing.branch = body.branch
    existing.encrypted_pat = encrypted_pat
    existing.updated_at = now
    session.flush()
    return {
        **_serialize_github_integration(existing, pat_plaintext=body.pat),
        "default_branch_from_github": meta.default_branch,
    }


@app.get("/workspaces/me/github")
def get_github_integration(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    row = session.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no GitHub integration registered")
    # Decrypt the PAT just to compute the display mask (first 4 + last 4).
    # Plaintext stays inside this function — the serializer always masks.
    try:
        pat_plaintext = secrets_crypto.decrypt(row.encrypted_pat)
    except Exception:
        pat_plaintext = None
    return _serialize_github_integration(row, pat_plaintext=pat_plaintext)


@app.delete("/workspaces/me/github")
def delete_github_integration(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    row = session.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.workspace_id == workspace_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="no GitHub integration registered")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


@app.get("/workspaces/me/github/agents")
def list_github_agent_paths(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(GitHubAgentPath)
        .where(GitHubAgentPath.workspace_id == workspace_id)
        .order_by(GitHubAgentPath.agent_name)
    ).scalars().all()
    return {"agents": [_serialize_github_agent_path(r) for r in rows]}


@app.put("/workspaces/me/github/agents/{agent_name}")
def put_github_agent_path(
    agent_name: str,
    body: GitHubAgentPathSetIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    if not GITHUB_AGENT_NAME_RE.match(agent_name):
        raise HTTPException(
            status_code=400,
            detail="agent_name must match [A-Za-z][A-Za-z0-9_-]{0,63}",
        )
    _validate_github_path(body.path)
    now = utcnow()
    existing = session.get(
        GitHubAgentPath, (workspace_id, agent_name)
    )
    if existing is None:
        row = GitHubAgentPath(
            workspace_id=workspace_id,
            agent_name=agent_name,
            path=body.path,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        existing.path = body.path
        existing.updated_at = now
        row = existing
    session.flush()
    return _serialize_github_agent_path(row)


@app.delete("/workspaces/me/github/agents/{agent_name}")
def delete_github_agent_path(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    if not GITHUB_AGENT_NAME_RE.match(agent_name):
        raise HTTPException(status_code=400, detail="malformed agent_name")
    row = session.get(GitHubAgentPath, (workspace_id, agent_name))
    if row is None:
        raise HTTPException(status_code=404, detail="agent path not registered")
    session.delete(row)
    session.flush()
    return {"status": "ok"}


# ---------- POST /webhooks/github (Phase 10.2) ---------- #
#
# Public endpoint — no Lightsei API key. GitHub posts here whenever a
# subscribed event fires on a registered repo. Authenticity is via
# HMAC-SHA256 of the raw body using the integration's webhook_secret
# (the one we revealed once in 10.1). We verify before doing anything
# state-changing.
#
# Order of operations:
#   1. Read raw body bytes (signature is over bytes, not the parsed dict).
#   2. Parse JSON to extract repo full_name — needed to find which
#      integration's secret to verify against.
#   3. Look up integration by (repo_owner, repo_name). 404 if none —
#      this also makes random scans noisy in logs without exposing
#      whether a workspace exists.
#   4. Decrypt the integration's webhook_secret and verify the
#      X-Hub-Signature-256 header. 401 on mismatch / missing.
#   5. Past this point, the request is authenticated. Branch into
#      event-type handlers. Anything we don't recognize 200s with a
#      `skipped` reason so GitHub stops retrying.
#
# We deliberately do NOT verify the signature before looking up the
# integration: we'd have nothing to verify against. The lookup itself
# is read-only and constant-time-ish (single PK-style index hit), so
# leaking "this repo is registered" via a 401-vs-404 differential is
# acceptable — repos on github.com are public knowledge anyway.


def _parse_repo_full_name(payload: dict[str, Any]) -> Optional[tuple[str, str]]:
    """Extract (owner, name) from a GitHub webhook payload's `repository`
    block. Returns None if the field is missing or malformed — caller
    treats that as a malformed body."""
    repo = payload.get("repository")
    if not isinstance(repo, dict):
        return None
    full_name = repo.get("full_name")
    if not isinstance(full_name, str) or "/" not in full_name:
        return None
    owner, _, name = full_name.partition("/")
    if not owner or not name:
        return None
    return owner, name


def _verify_github_signature(
    *, raw_body: bytes, header_value: Optional[str], secret: str
) -> bool:
    """Constant-time-compare X-Hub-Signature-256 against HMAC of body.
    GitHub sends the value as `sha256=<hex>`. Anything else (missing
    header, wrong prefix, malformed hex) is a verification failure."""
    if not header_value or not header_value.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = header_value.removeprefix("sha256=").strip()
    return hmac.compare_digest(expected, provided)


def _push_touched_path(commits: list[dict[str, Any]], agent_path: str) -> bool:
    """True if any commit in the push touched a file under `agent_path`.

    A path "matches" a commit if any of `added/modified/removed` is
    either equal to `agent_path` (exact-file mapping) or starts with
    `agent_path + "/"` (directory mapping). We don't try to be clever
    about renames — GitHub reports renames as a remove + add pair, so
    they're already covered."""
    if not isinstance(commits, list):
        return False
    prefix = agent_path.rstrip("/") + "/"
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for field in ("added", "modified", "removed"):
            files = commit.get(field) or []
            if not isinstance(files, list):
                continue
            for f in files:
                if not isinstance(f, str):
                    continue
                if f == agent_path or f.startswith(prefix):
                    return True
    return False


def _collect_touched_paths(commits: list[dict[str, Any]]) -> list[str]:
    """Flatten added/modified/removed across all commits in a push.

    Polaris's evaluate_push handler matches these against POLARIS_PUSH_RULES
    glob patterns to decide which downstream agents to dispatch. Order is
    preserved, duplicates removed (a single file changed across multiple
    commits in one push only counts once for rule matching).
    """
    if not isinstance(commits, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for commit in commits:
        if not isinstance(commit, dict):
            continue
        for field in ("added", "modified", "removed"):
            files = commit.get(field) or []
            if not isinstance(files, list):
                continue
            for f in files:
                if isinstance(f, str) and f and f not in seen:
                    seen.add(f)
                    out.append(f)
    return out


def _retire_active_deployments_for_agent(
    session: Session,
    *,
    workspace_id: str,
    agent_name: str,
) -> int:
    """Flip `desired_state` to 'stopped' on every existing deployment for
    `(workspace_id, agent_name)` that's still queued / building / running.

    Called immediately before persisting a NEW deployment for the same
    agent, so the worker's claim loop + the running supervisor's
    desired-state poll cooperate to retire the old bundle and free the
    concurrency slot for the new one. Without this, every redeploy
    accumulates an orphan bot until MAX_CONCURRENT is hit and queues
    silently stall (the issue we worked around with manual `pkill`
    during the Phase 11.7 demo and the Phase 12.4 polaris dance).

    Returns the count of rows retired so the caller can log it.
    """
    rows = (
        session.execute(
            select(Deployment).where(
                Deployment.workspace_id == workspace_id,
                Deployment.agent_name == agent_name,
                Deployment.desired_state == "running",
                Deployment.status.in_(["queued", "building", "running"]),
            )
        )
        .scalars()
        .all()
    )
    now = utcnow()
    for d in rows:
        d.desired_state = "stopped"
        d.updated_at = now
    if rows:
        session.flush()
    return len(rows)


def _queue_github_redeploy(
    session: Session,
    *,
    integration: GitHubIntegration,
    agent_name: str,
    agent_path: str,
    commit_sha: str,
) -> Optional[str]:
    """Fetch the agent's directory at `commit_sha` via the GitHub
    Contents API, store the zip as a DeploymentBlob, and create a
    Deployment row with `source='github_push'` and
    `desired_state='running'` so the Phase 5 worker picks it up via
    its existing claim loop. Returns the new deployment id, or None if
    the fetch failed (logged but does not fail the webhook — GitHub
    would otherwise retry on transient errors and we'd dispatch the
    same push twice).

    Errors fall into two buckets:
      - GitHubAPIError: PAT lost access, repo deleted mid-flight, the
        agent path no longer exists, etc. We swallow and return None;
        the dashboard's Deployments panel will simply show no new row,
        and the user can re-trigger via `lightsei deploy` or by
        re-pushing.
      - Anything else (zip codec, DB IntegrityError): re-raised so it
        surfaces in the webhook response as a 500 — these are bugs in
        our code, not user error, and we want them noisy.
    """
    try:
        pat = secrets_crypto.decrypt(integration.encrypted_pat)
    except Exception:
        # Decryption failure is a server config issue (rotated key,
        # corrupted row). Don't try to deploy; log and let the user
        # re-register.
        return None

    try:
        zip_bytes = github_api.fetch_directory_zip(
            repo_owner=integration.repo_owner,
            repo_name=integration.repo_name,
            commit_sha=commit_sha,
            path=agent_path,
            pat=pat,
        )
    except github_api.GitHubAPIError:
        # Couldn't fetch the directory. The webhook stays a 200 because
        # GitHub already gave us all the data they have — they can't
        # help by retrying. The caller (webhook receiver) records this
        # as a non-deployment in the response.
        return None

    if len(zip_bytes) == 0:
        # Empty agent dir at this commit. Don't create a deploy with
        # nothing to run.
        return None

    now = utcnow()
    sha = hashlib.sha256(zip_bytes).hexdigest()

    blob = DeploymentBlob(
        id=str(uuid.uuid4()),
        workspace_id=integration.workspace_id,
        size_bytes=len(zip_bytes),
        sha256=sha,
        data=zip_bytes,
        created_at=now,
    )
    session.add(blob)
    session.flush()

    ensure_agent(session, integration.workspace_id, agent_name, now)
    _retire_active_deployments_for_agent(
        session,
        workspace_id=integration.workspace_id,
        agent_name=agent_name,
    )
    dep = Deployment(
        id=str(uuid.uuid4()),
        workspace_id=integration.workspace_id,
        agent_name=agent_name,
        status="queued",
        desired_state="running",
        source_blob_id=blob.id,
        source="github_push",
        source_commit_sha=commit_sha,
        created_at=now,
        updated_at=now,
    )
    session.add(dep)
    session.flush()
    return dep.id


@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    raw_body = await request.body()
    try:
        payload = json.loads(raw_body)
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="body is not valid JSON")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body is not a JSON object")

    parsed = _parse_repo_full_name(payload)
    if parsed is None:
        raise HTTPException(
            status_code=400, detail="payload missing repository.full_name"
        )
    owner, name = parsed

    integration = session.execute(
        select(GitHubIntegration).where(
            GitHubIntegration.repo_owner == owner,
            GitHubIntegration.repo_name == name,
        )
    ).scalar_one_or_none()
    if integration is None:
        # No workspace has registered this repo. Tell GitHub explicitly
        # so the webhook surfaces the misconfiguration in its UI rather
        # than silently swallowing.
        raise HTTPException(
            status_code=404, detail=f"no integration for {owner}/{name}"
        )

    try:
        webhook_secret = secrets_crypto.decrypt(integration.encrypted_webhook_secret)
    except Exception:
        # Encryption-key rotation gone wrong, or DB row corruption. We
        # can't verify the signature without the secret, so we can't
        # trust anything in the body. 500 because this is a server
        # config issue, not a caller issue.
        raise HTTPException(
            status_code=500, detail="integration secret unavailable"
        )

    sig_header = request.headers.get("x-hub-signature-256")
    if not _verify_github_signature(
        raw_body=raw_body, header_value=sig_header, secret=webhook_secret
    ):
        raise HTTPException(status_code=401, detail="invalid signature")

    # ----- past this line the request is authenticated ----- #

    if not integration.is_active:
        # User disabled the integration but the GitHub webhook is
        # still pointed at us. Quietly accept so GitHub doesn't retry,
        # but do nothing.
        return {
            "status": "ok",
            "event": request.headers.get("x-github-event"),
            "skipped": "integration_inactive",
        }

    event_type = request.headers.get("x-github-event") or "unknown"

    if event_type == "ping":
        # GitHub fires `ping` immediately after webhook creation. The
        # zen quote in the body is meaningless to us; just acknowledge.
        return {"status": "ok", "event": "ping"}

    if event_type != "push":
        # Tag pushes (`create`), branch deletes (`delete`), PRs, etc.
        # Phase 10 only handles `push`. 200 with a hint so the user
        # can debug from the GitHub webhook log if they were expecting
        # a deploy.
        return {
            "status": "ok",
            "event": event_type,
            "skipped": "event_type_not_handled",
        }

    ref = payload.get("ref")
    expected_ref = f"refs/heads/{integration.branch}"
    if ref != expected_ref:
        return {
            "status": "ok",
            "event": "push",
            "skipped": "branch_not_tracked",
            "ref": ref,
            "tracked_branch": integration.branch,
        }

    head_commit = payload.get("head_commit") or {}
    commit_sha = (
        payload.get("after")
        or (head_commit.get("id") if isinstance(head_commit, dict) else None)
        or ""
    )
    if not isinstance(commit_sha, str):
        commit_sha = ""

    commits = payload.get("commits")
    paths = (
        session.execute(
            select(GitHubAgentPath).where(
                GitHubAgentPath.workspace_id == integration.workspace_id
            )
        )
        .scalars()
        .all()
    )

    queued: list[dict[str, Any]] = []
    for ap in paths:
        if _push_touched_path(commits or [], ap.path):
            deployment_id = _queue_github_redeploy(
                session,
                integration=integration,
                agent_name=ap.agent_name,
                agent_path=ap.path,
                commit_sha=commit_sha,
            )
            queued.append(
                {
                    "agent_name": ap.agent_name,
                    "commit_sha": commit_sha,
                    "deployment_id": deployment_id,
                }
            )

    # Phase 11.5: enqueue a `polaris.evaluate_push` command so Polaris
    # gets an event-driven dispatch path on every push (in addition to
    # its hourly tick). Polaris's handler reads POLARIS_PUSH_RULES to
    # decide whether to dispatch downstream commands (e.g. atlas.run_tests
    # when backend code changed) without burning a Claude call.
    #
    # Chain id is the GitHub delivery id so every command in the chain
    # — this evaluate_push command, the atlas.run_tests Polaris dispatches,
    # the hermes.post Atlas dispatches — share one chain that the 11.6
    # /dispatch view can render as a single tree.
    delivery_id = (request.headers.get("x-github-delivery") or "").strip()
    chain_id = delivery_id or str(uuid.uuid4())
    touched_paths = _collect_touched_paths(commits or [])
    head_author = (
        head_commit.get("author") if isinstance(head_commit, dict) else None
    )
    push_payload = {
        "commit_sha": commit_sha,
        "branch": integration.branch,
        "repo": f"{owner}/{name}",
        "touched_paths": touched_paths,
        "author": head_author if isinstance(head_author, dict) else None,
        "delivery_id": delivery_id or None,
    }
    ensure_agent(session, integration.workspace_id, "polaris", utcnow())
    approval_state = _resolve_auto_approval(
        session,
        workspace_id=integration.workspace_id,
        source_agent=None,
        target_agent="polaris",
        command_kind="polaris.evaluate_push",
    )
    now = utcnow()
    polaris_cmd = Command(
        id=str(uuid.uuid4()),
        workspace_id=integration.workspace_id,
        agent_name="polaris",
        kind="polaris.evaluate_push",
        payload=push_payload,
        status="pending",
        source_agent=None,
        dispatch_chain_id=chain_id,
        dispatch_depth=0,
        approval_state=approval_state,
        approved_at=now if approval_state == "auto_approved" else None,
        created_at=now,
        expires_at=now + COMMAND_TTL,
    )
    session.add(polaris_cmd)
    session.flush()

    return {
        "status": "ok",
        "event": "push",
        "ref": ref,
        "commit_sha": commit_sha,
        "queued_redeploys": queued,
        "polaris_command_id": polaris_cmd.id,
        "dispatch_chain_id": chain_id,
    }


# ---------- /workspaces/me/deployments (Phase 5.1) ---------- #
#
# A "deployment" is a snapshot of bot source code (zipped) plus the lifecycle
# state of one running copy of it. Phase 5.1 only covers the upload + listing
# surface — claim/heartbeat/log endpoints land in 5.2 and the worker process
# in 5.3.


def _serialize_deployment(d: Deployment) -> dict[str, Any]:
    return {
        "id": d.id,
        "agent_name": d.agent_name,
        "status": d.status,
        "desired_state": d.desired_state,
        "source_blob_id": d.source_blob_id,
        "source": d.source,
        "source_commit_sha": d.source_commit_sha,
        "error": d.error,
        "claimed_by": d.claimed_by,
        "claimed_at": d.claimed_at.isoformat() if d.claimed_at else None,
        "heartbeat_at": d.heartbeat_at.isoformat() if d.heartbeat_at else None,
        "started_at": d.started_at.isoformat() if d.started_at else None,
        "stopped_at": d.stopped_at.isoformat() if d.stopped_at else None,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
    }


@app.post("/workspaces/me/deployments")
async def upload_deployment(
    agent_name: str = Form(..., min_length=1),
    bundle: UploadFile = File(...),
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Upload a bundled bot directory as a new deployment.

    The bundle is a gzipped tar (or zip) the SDK CLI produced from a local
    directory. We store the bytes verbatim in deployment_blobs and create a
    `queued` deployment row that the worker (Phase 5.3) will pick up.

    Bundle is read in full so we can compute size + hash up front. The
    multipart cap (10 MB) is enforced by BodySizeLimitMiddleware before
    we get here; this is a defense-in-depth check.
    """
    data = await bundle.read()
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="empty bundle")
    if len(data) > MAX_DEPLOYMENT_BLOB_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"bundle too large; max {MAX_DEPLOYMENT_BLOB_BYTES} bytes",
        )

    now = utcnow()
    sha = hashlib.sha256(data).hexdigest()

    blob = DeploymentBlob(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        size_bytes=len(data),
        sha256=sha,
        data=data,
        created_at=now,
    )
    session.add(blob)
    session.flush()

    ensure_agent(session, workspace_id, agent_name, now)
    _retire_active_deployments_for_agent(
        session, workspace_id=workspace_id, agent_name=agent_name,
    )
    dep = Deployment(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        status="queued",
        desired_state="running",
        source_blob_id=blob.id,
        source="cli",
        source_commit_sha=None,
        created_at=now,
        updated_at=now,
    )
    session.add(dep)
    session.flush()
    return _serialize_deployment(dep)


@app.get("/workspaces/me/deployments")
def list_deployments(
    agent_name: Optional[str] = None,
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """List deployments scoped to this workspace, newest first.
    Optionally filter to one agent."""
    limit = max(1, min(limit, 200))
    q = select(Deployment).where(Deployment.workspace_id == workspace_id)
    if agent_name is not None:
        q = q.where(Deployment.agent_name == agent_name)
    rows = session.execute(
        q.order_by(desc(Deployment.created_at)).limit(limit)
    ).scalars().all()
    return {"deployments": [_serialize_deployment(d) for d in rows]}


@app.get("/workspaces/me/deployments/{deployment_id}")
def get_deployment(
    deployment_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    d = session.get(Deployment, deployment_id)
    if d is None or d.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    return _serialize_deployment(d)


@app.get("/workspaces/me/deployments/{deployment_id}/logs")
def get_deployment_logs(
    deployment_id: str,
    after_id: int = 0,
    limit: int = 200,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Tail-style log fetch. Pass `after_id` to get only lines newer than the
    last id you saw. `limit` caps the response (1-1000)."""
    limit = max(1, min(limit, 1000))
    dep = session.get(Deployment, deployment_id)
    if dep is None or dep.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    rows = session.execute(
        select(DeploymentLog)
        .where(
            DeploymentLog.deployment_id == deployment_id,
            DeploymentLog.id > after_id,
        )
        .order_by(DeploymentLog.id)
        .limit(limit)
    ).scalars().all()
    return {
        "lines": [
            {
                "id": r.id,
                "ts": r.ts.isoformat(),
                "stream": r.stream,
                "line": r.line,
            }
            for r in rows
        ],
        "max_id": rows[-1].id if rows else after_id,
    }


@app.post("/workspaces/me/deployments/{deployment_id}/stop")
def stop_deployment(
    deployment_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Flip desired_state to stopped. The worker's heartbeat picks it up
    within ~30s and terminates the bot."""
    dep = session.get(Deployment, deployment_id)
    if dep is None or dep.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    dep.desired_state = "stopped"
    dep.updated_at = utcnow()
    session.flush()
    return _serialize_deployment(dep)


@app.post("/workspaces/me/deployments/{deployment_id}/redeploy")
def redeploy_deployment(
    deployment_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Create a new deployment pointing at the same source blob. The old
    deployment is stopped (desired_state=stopped) so the worker swaps over
    cleanly. Useful for restarting a wedged bot without re-uploading."""
    old = session.get(Deployment, deployment_id)
    if old is None or old.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    if old.source_blob_id is None:
        raise HTTPException(
            status_code=400,
            detail="deployment has no source blob to redeploy from",
        )
    now = utcnow()
    old.desired_state = "stopped"
    old.updated_at = now

    new = Deployment(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=old.agent_name,
        status="queued",
        desired_state="running",
        source_blob_id=old.source_blob_id,
        # Carry the original provenance forward — a redeploy reuses the
        # same source blob, so the same commit/upload origin still
        # applies. Otherwise a "redeploy" of a github_push deploy would
        # falsely look like a CLI upload in the dashboard.
        source=old.source,
        source_commit_sha=old.source_commit_sha,
        created_at=now,
        updated_at=now,
    )
    session.add(new)
    session.flush()
    return _serialize_deployment(new)


@app.delete("/workspaces/me/deployments/{deployment_id}")
def delete_deployment(
    deployment_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    """Delete a deployment row. The associated blob is removed too unless
    other deployments reference it (none should, but the FK uses SET NULL
    so an orphaned blob would just sit until an explicit prune)."""
    d = session.get(Deployment, deployment_id)
    if d is None or d.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="deployment not found")
    blob_id = d.source_blob_id
    session.delete(d)
    session.flush()  # so the still_used query sees the deletion
    if blob_id:
        # Only purge the blob if no other deployment references it.
        still_used = session.execute(
            select(Deployment.id)
            .where(Deployment.source_blob_id == blob_id)
            .limit(1)
        ).scalar_one_or_none()
        if still_used is None:
            blob = session.get(DeploymentBlob, blob_id)
            if blob is not None:
                session.delete(blob)
    session.flush()
    return {"status": "ok"}


# ---------- /worker/* (Phase 5.2) ---------- #
#
# Worker-facing surface. All endpoints require a bearer matching
# LIGHTSEI_WORKER_TOKEN — see backend/worker_auth.py for the threat model.
# The worker is treated as a trusted system component; a stolen token grants
# cross-tenant access.


class WorkerStatusUpdateIn(BaseModel):
    status: str = Field(min_length=1)
    error: Optional[str] = None


class WorkerLogLineIn(BaseModel):
    ts: Optional[datetime] = None
    stream: str = Field(default="stdout")
    line: str = Field(default="")


class WorkerLogAppendIn(BaseModel):
    lines: list[WorkerLogLineIn] = Field(default_factory=list)


@app.post("/worker/deployments/claim")
def worker_claim_deployment(
    worker_id: str,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, Any]:
    """Atomically grab the oldest queued (or stale-claimed) deployment whose
    user wants it running. SKIP LOCKED matches the pattern used for commands
    and chat turns.

    "Stale-claimed" means another worker took it but hasn't heartbeated within
    WORKER_CLAIM_TTL — almost certainly that worker died. Re-claiming is safe
    because the dead worker can't possibly be running a bot anymore.
    """
    now = utcnow()
    stale_cutoff = now - WORKER_CLAIM_TTL
    row = session.execute(
        text(
            """
            SELECT id FROM deployments
            WHERE desired_state = 'running'
              AND status IN ('queued', 'building', 'running')
              AND (claimed_by IS NULL OR heartbeat_at < :stale_cutoff)
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"stale_cutoff": stale_cutoff},
    ).first()
    if row is None:
        return {"deployment": None}
    dep = session.get(Deployment, row.id)
    if dep is None:
        return {"deployment": None}
    dep.claimed_by = worker_id
    dep.claimed_at = now
    dep.heartbeat_at = now
    if dep.status == "queued":
        dep.status = "building"
    dep.updated_at = now
    session.flush()
    return {
        "deployment": _serialize_deployment(dep),
        "workspace_id": dep.workspace_id,
    }


@app.post("/worker/deployments/{deployment_id}/status")
def worker_update_status(
    deployment_id: str,
    body: WorkerStatusUpdateIn,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, Any]:
    """Worker reports a status transition. Sets started_at/stopped_at as
    appropriate. Invalid transitions are accepted (server doesn't know what
    the worker is doing) but the status string is checked against the known
    enum to catch typos."""
    valid = {"queued", "building", "running", "stopped", "failed"}
    if body.status not in valid:
        raise HTTPException(
            status_code=400,
            detail=f"unknown status {body.status!r}; must be one of {sorted(valid)}",
        )
    dep = session.get(Deployment, deployment_id)
    if dep is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    now = utcnow()
    dep.status = body.status
    dep.error = body.error
    dep.heartbeat_at = now
    if body.status == "running" and dep.started_at is None:
        dep.started_at = now
    if body.status in ("stopped", "failed"):
        dep.stopped_at = now
    dep.updated_at = now
    session.flush()
    return _serialize_deployment(dep)


@app.post("/worker/deployments/{deployment_id}/heartbeat")
def worker_heartbeat(
    deployment_id: str,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, Any]:
    """Refresh the worker's claim. Returns the current deployment row so the
    worker can see whether `desired_state` flipped (e.g. user clicked stop)
    without a separate fetch."""
    dep = session.get(Deployment, deployment_id)
    if dep is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    dep.heartbeat_at = utcnow()
    session.flush()
    return _serialize_deployment(dep)


@app.post("/worker/deployments/{deployment_id}/logs")
def worker_append_logs(
    deployment_id: str,
    body: WorkerLogAppendIn,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, int]:
    """Append log lines. Bounded to MAX_DEPLOYMENT_LOG_LINES per deployment
    by deleting the oldest rows after each insert."""
    dep = session.get(Deployment, deployment_id)
    if dep is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    if not body.lines:
        return {"appended": 0}
    now = utcnow()
    rows = [
        DeploymentLog(
            deployment_id=deployment_id,
            ts=line.ts or now,
            stream=line.stream,
            line=line.line,
        )
        for line in body.lines
    ]
    session.add_all(rows)
    session.flush()
    # Prune older rows beyond the cap. Keep this cheap: count + DELETE.
    count = session.execute(
        select(text("COUNT(*)")).select_from(DeploymentLog)
        .where(DeploymentLog.deployment_id == deployment_id)
    ).scalar_one()
    overflow = count - MAX_DEPLOYMENT_LOG_LINES
    if overflow > 0:
        session.execute(
            text(
                """
                DELETE FROM deployment_logs
                WHERE id IN (
                  SELECT id FROM deployment_logs
                  WHERE deployment_id = :dep
                  ORDER BY id ASC LIMIT :n
                )
                """
            ),
            {"dep": deployment_id, "n": overflow},
        )
    return {"appended": len(rows)}


@app.get("/worker/blobs/{blob_id}")
def worker_get_blob(
    blob_id: str,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
):
    """Return the raw bytes of a deployment blob (the zipped bot directory)."""
    blob = session.get(DeploymentBlob, blob_id)
    if blob is None:
        raise HTTPException(status_code=404, detail="blob not found")
    from fastapi.responses import Response as RawResponse
    return RawResponse(
        content=blob.data,
        media_type="application/octet-stream",
        headers={
            "x-lightsei-blob-sha256": blob.sha256,
            "x-lightsei-blob-size": str(blob.size_bytes),
        },
    )


@app.get("/worker/workspaces/{workspace_id}/secrets")
def worker_list_workspace_secrets(
    workspace_id: str,
    session: Session = Depends(get_session),
    _: None = Depends(get_worker),
) -> dict[str, Any]:
    """Decrypted secrets for a workspace, indexed by name. The worker
    injects these as env vars when spawning a bot."""
    if not secrets_crypto.is_available():
        raise HTTPException(
            status_code=503,
            detail="secrets store unavailable: LIGHTSEI_SECRETS_KEY is not configured",
        )
    rows = session.execute(
        select(WorkspaceSecret).where(WorkspaceSecret.workspace_id == workspace_id)
    ).scalars().all()
    out: dict[str, str] = {}
    for s in rows:
        try:
            out[s.name] = secrets_crypto.decrypt(s.encrypted_value)
        except Exception:
            # A single corrupt row should not poison the rest of the dict.
            # The user will see the bot fail to read this secret and can
            # re-set it from the dashboard.
            continue
    return {"secrets": out}


# ---------- /auth ---------- #

def _create_session(session: Session, user: User) -> tuple[SessionRow, str]:
    plaintext = generate_session_token()
    now = utcnow()
    row = SessionRow(
        id=str(uuid.uuid4()),
        user_id=user.id,
        token_hash=hash_token(plaintext),
        created_at=now,
        expires_at=now + SESSION_TTL,
    )
    session.add(row)
    session.flush()
    return row, plaintext


def _serialize_user(u: User) -> dict[str, Any]:
    return {
        "id": u.id,
        "email": u.email,
        "workspace_id": u.workspace_id,
        "created_at": u.created_at.isoformat(),
    }


@app.post("/auth/signup")
def signup(
    body: SignupIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    limit_signup_attempt(request)
    existing = session.execute(
        select(User).where(User.email == body.email.lower())
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail="email already registered")

    now = utcnow()
    ws = Workspace(id=str(uuid.uuid4()), name=body.workspace_name, created_at=now)
    session.add(ws)
    session.flush()
    seed_default_validators(session, ws.id, now)

    user = User(
        id=str(uuid.uuid4()),
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        workspace_id=ws.id,
        created_at=now,
    )
    session.add(user)
    session.flush()  # need user.id present before sessions row FKs to it

    plaintext_key = generate_key()
    api_key_row = ApiKey(
        id=str(uuid.uuid4()),
        workspace_id=ws.id,
        name="default",
        prefix=prefix_for_display(plaintext_key),
        hash=hash_token(plaintext_key),
        created_at=now,
    )
    session.add(api_key_row)

    sess_row, sess_plain = _create_session(session, user)

    return {
        "user": _serialize_user(user),
        "workspace": _serialize_workspace(ws),
        "api_key": _serialize_api_key(api_key_row) | {"plaintext": plaintext_key},
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
    }


@app.post("/auth/login")
def login(
    body: LoginIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    limit_login_attempt(request)
    user = session.execute(
        select(User).where(User.email == body.email.lower())
    ).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid email or password")
    sess_row, sess_plain = _create_session(session, user)
    ws = session.get(Workspace, user.workspace_id)
    return {
        "user": _serialize_user(user),
        "workspace": _serialize_workspace(ws) if ws else None,
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
    }


@app.post("/auth/logout")
def logout(
    auth: AuthResult = Depends(get_authenticated),
) -> dict[str, Any]:
    if auth.session is None:
        raise HTTPException(status_code=400, detail="logout requires a session token")
    if auth.session.revoked_at is None:
        auth.session.revoked_at = utcnow()
    return {"status": "ok"}


# ---------- Phase 17.2: magic-link auth ---------- #


# How many magic-link requests we accept for a single email per hour.
# Tight enough that a malicious sender can't spam someone's inbox,
# loose enough that a confused user clicking "send again" 2-3 times
# in a row still works.
MAGIC_LINK_MAX_PER_HOUR = 5

# How long a fresh token is valid. 15 minutes is plenty for the user
# to switch to their email and click; short enough that a leaked
# email backup doesn't hand attackers active sign-in.
MAGIC_LINK_TTL = timedelta(minutes=15)


def _hash_magic_token(token: str) -> str:
    """Same sha256-hex pattern keys.hash_token uses. Defined here to
    avoid coupling the magic-link flow to the API-key key derivation —
    if either changes the other shouldn't have to follow."""
    import hashlib as _hashlib
    return _hashlib.sha256(token.encode("utf-8")).hexdigest()


def _dashboard_base_url() -> str:
    """Where the magic-link URL points. Env-overridable so local dev
    can use localhost; defaults to the prod dashboard."""
    return os.environ.get(
        "LIGHTSEI_DASHBOARD_URL", "https://app.lightsei.com",
    )


def _workspace_name_for_signup_email(email: str) -> str:
    """A friendly default workspace name from the user's email local
    part — `alice@example.com` → "alice's workspace". The user can
    rename via /account afterward."""
    local = email.split("@", 1)[0]
    return f"{local}'s workspace"


@app.post("/auth/magic-link/request")
def request_magic_link(
    body: MagicLinkRequestIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.2: kick off a magic-link sign-in.

    Always returns 200 — don't leak whether the email is registered.
    Rate-limited per-IP (existing limit_signup_attempt machinery)
    AND per-email (count in the last hour vs MAGIC_LINK_MAX_PER_HOUR)
    so a malicious sender can't spam a single inbox via rotating IPs.

    On success, inserts an email_signin_tokens row with a 15-minute
    TTL and sends a Resend email containing the unhashed token in a
    magic URL. The dashboard's /auth/magic-link page POSTs the
    token back to /auth/magic-link/consume.
    """
    import email_provider as _email_mod

    limit_signup_attempt(request)
    email_addr = body.email.lower()
    now = utcnow()

    # Per-email rate-limit: count tokens we've issued in the last hour.
    # An hour is enough that 5 attempts feels generous; a malicious
    # sender hitting the limit means the user already has 5 fresh
    # magic links in their inbox.
    cutoff = now - timedelta(hours=1)
    recent_count = session.execute(
        select(func.count(EmailSigninToken.token_hash)).where(
            EmailSigninToken.email == email_addr,
            EmailSigninToken.created_at >= cutoff,
        )
    ).scalar_one()
    if recent_count >= MAGIC_LINK_MAX_PER_HOUR:
        # Same always-200 contract so we don't leak that this email
        # is being targeted.
        return {"status": "ok"}

    # Generate token + insert hashed row. Same plain-token-in-URL +
    # hashed-row-in-DB pattern the existing API keys use.
    plaintext = _stdlib_secrets.token_urlsafe(32)
    token_hash = _hash_magic_token(plaintext)
    session.add(EmailSigninToken(
        token_hash=token_hash,
        email=email_addr,
        created_at=now,
        expires_at=now + MAGIC_LINK_TTL,
    ))
    session.flush()

    try:
        _email_mod.send_magic_link(
            email=email_addr,
            token=plaintext,
            dashboard_url=_dashboard_base_url(),
        )
    except Exception:
        # Best-effort email send. Logged inside the module. We still
        # return 200 to preserve the no-leak contract; user can
        # retry from the dashboard if the email never arrives.
        import logging as _logging
        _logging.getLogger("lightsei.auth").warning(
            "magic-link request: send failed for %s — token still "
            "valid via direct backend POST",
            email_addr,
        )
    return {"status": "ok"}


@app.post("/auth/magic-link/consume")
def consume_magic_link(
    body: MagicLinkConsumeIn,
    request: Request,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.2: consume a magic-link token, sign in (or create) the
    user, return a session.

    422 on unknown / expired / consumed token (don't distinguish — the
    user can't action the difference and we don't want token-validity
    probes to leak existence). 200 with a session token on success.

    New-user path creates a User + Workspace pair the same way the
    existing /auth/signup does, minus the password (auth_provider =
    'magic_link', email_verified = True since they proved control of
    the inbox). No API key is auto-created — non-technical users don't
    need one; developers can generate one from /account.
    """
    limit_login_attempt(request)
    token_hash = _hash_magic_token(body.token)
    now = utcnow()

    row = session.get(EmailSigninToken, token_hash)
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        # Single 422 covers all the rejection paths so the client
        # can't probe for "exists but expired" vs "never existed".
        raise HTTPException(
            status_code=422,
            detail="magic-link token is invalid or expired — request a new one",
        )

    # Mark consumed before doing anything else so a parallel POST with
    # the same token gets rejected by the next check. Single-use.
    row.consumed_at = now
    session.flush()

    email_addr = row.email
    user = session.execute(
        select(User).where(User.email == email_addr)
    ).scalar_one_or_none()
    is_new_user = user is None

    if user is None:
        # Fresh signup via magic link. Workspace gets the same starting
        # state Phase 17.1's backfill applied to existing rows: free
        # tier + $5 in credits. No API key auto-generated — magic-link
        # users land on the dashboard, not the SDK.
        ws = Workspace(
            id=str(uuid.uuid4()),
            name=_workspace_name_for_signup_email(email_addr),
            created_at=now,
            plan_tier="free",
            free_credits_remaining_usd=Decimal("5.00"),
        )
        session.add(ws)
        session.flush()
        seed_default_validators(session, ws.id, now)

        user = User(
            id=str(uuid.uuid4()),
            email=email_addr,
            # No password — magic-link users can't fall back to the
            # password login. A non-empty placeholder so the NOT NULL
            # constraint passes; it's not a real bcrypt hash so the
            # /auth/login verify_password will fail-closed for them.
            password_hash="magic-link-only:no-password",
            workspace_id=ws.id,
            created_at=now,
            email_verified=True,
            auth_provider="magic_link",
        )
        session.add(user)
        session.flush()
    else:
        # Existing user signing in via magic link — promote them to
        # verified if they weren't already. Doesn't change the original
        # auth_provider; the user keeps whatever path created them.
        if not user.email_verified:
            user.email_verified = True

    sess_row, sess_plain = _create_session(session, user)
    ws = session.get(Workspace, user.workspace_id)

    return {
        "user": _serialize_user(user),
        "workspace": _serialize_workspace(ws) if ws else None,
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
        "is_new_user": is_new_user,
    }


# ---------- Phase 17.3: Google OAuth ---------- #


# How long the (state, code_verifier) row in oauth_pending_states is
# valid. Has to cover the user's hop out to Google's consent screen +
# back. 10 minutes is generous; reduce later if abandoned-flow rows
# pile up.
OAUTH_STATE_TTL = timedelta(minutes=10)


@app.get("/auth/google/start")
def google_oauth_start(
    redirect_after: Optional[str] = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.3: kick off Google OAuth.

    Returns `{authorization_url, state}` — the dashboard navigates the
    browser to authorization_url. State + PKCE verifier are persisted
    so /auth/google/callback can rebind on return.

    503 if the Google OAuth client isn't configured (env vars not set);
    fail loud rather than redirecting the user into a half-configured
    flow that errors out on Google's end with a less actionable message.

    `redirect_after` is where the dashboard wants the user to land
    post-signin (e.g. the page they tried to reach signed-out). Optional;
    default is `/`.
    """
    import google_oauth as _g

    if not _g.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not configured on this backend. Set "
                "LIGHTSEI_GOOGLE_CLIENT_ID + LIGHTSEI_GOOGLE_CLIENT_SECRET."
            ),
        )

    now = utcnow()
    verifier, challenge = _g.new_pkce_pair()
    state = _g.new_state()

    session.add(OAuthPendingState(
        state=state,
        code_verifier=verifier,
        redirect_after=redirect_after,
        created_at=now,
        expires_at=now + OAUTH_STATE_TTL,
    ))
    session.flush()

    return {
        "authorization_url": _g.build_authorization_url(
            state=state, challenge=challenge,
        ),
        "state": state,
    }


@app.get("/auth/google/callback")
def google_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.3: complete Google OAuth.

    Validates state, exchanges code for tokens, fetches userinfo,
    matches a returning user (by google_user_id exact, fallback to
    verified-email) or creates a fresh user+workspace pair.

    Error paths return 400 with a JSON body the dashboard's callback
    page can render directly. Unknown / expired state → 400 (treats
    the same so we don't leak which one).
    """
    import google_oauth as _g

    if error:
        # Google can hand back ?error=access_denied if the user cancelled
        # on the consent screen. Surface a clean 400; the dashboard
        # callback page renders "Sign-in cancelled" and links back to
        # /login.
        raise HTTPException(
            status_code=400,
            detail={"error": error, "message": "Google sign-in was cancelled or refused."},
        )

    if not code or not state:
        raise HTTPException(
            status_code=400,
            detail={"error": "missing_params", "message": "missing code or state"},
        )

    pending = session.get(OAuthPendingState, state)
    now = utcnow()
    if pending is None or pending.expires_at <= now:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_state",
                "message": "OAuth state is unknown or expired — start the sign-in flow again.",
            },
        )

    # Single-use: delete the pending row before doing the exchange so
    # a parallel callback with the same state can't double-consume.
    verifier = pending.code_verifier
    redirect_after = pending.redirect_after or "/"
    session.delete(pending)
    session.flush()

    try:
        claims = _g.exchange_code_for_userinfo(
            code=code, code_verifier=verifier,
        )
    except _g.GoogleOAuthError as exc:
        # Logged inside the helper; surface a user-friendly 400.
        raise HTTPException(
            status_code=400,
            detail={
                "error": "exchange_failed",
                "message": "Google sign-in didn't complete. Try again.",
                "_debug": str(exc),
            },
        )

    sub = claims["sub"]
    email = claims["email"]
    email_verified = claims["email_verified"]

    # Match priority: google_user_id exact → verified-email → new user.
    user = session.execute(
        select(User).where(User.google_user_id == sub)
    ).scalar_one_or_none()

    if user is None and email_verified:
        # Returning email-known user (e.g. signed up via magic-link or
        # apikey first, now connecting Google). Link the google_user_id
        # so future logins skip the email-match path.
        user = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if user is not None:
            user.google_user_id = sub

    is_new_user = user is None

    if user is None:
        # Before creating: if the email is already in use by a user
        # that DIDN'T match by sub OR verified-email (i.e. we got here
        # because Google said the email is unverified and the existing
        # user is a different identity), refuse cleanly instead of
        # crashing on the unique-email constraint. Forces the user to
        # sign in via the path that originally created the account
        # (which is the right user-facing behavior — we don't want
        # an unverified Google email to take over a real account).
        existing_with_email = session.execute(
            select(User).where(User.email == email)
        ).scalar_one_or_none()
        if existing_with_email is not None:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "email_already_in_use",
                    "message": (
                        "An account with this email already exists, but "
                        "the email on your Google account isn't verified. "
                        "Sign in with the original method (magic link or "
                        "password), then connect Google from /account."
                    ),
                },
            )

        ws = Workspace(
            id=str(uuid.uuid4()),
            name=_workspace_name_for_signup_email(email),
            created_at=now,
            plan_tier="free",
            free_credits_remaining_usd=Decimal("5.00"),
        )
        session.add(ws)
        session.flush()
        seed_default_validators(session, ws.id, now)

        user = User(
            id=str(uuid.uuid4()),
            email=email,
            # No password — OAuth-only users can't fall back to
            # /auth/login. Same placeholder pattern magic-link uses.
            password_hash="oauth-only:no-password",
            workspace_id=ws.id,
            created_at=now,
            email_verified=email_verified,
            auth_provider="google_oauth",
            google_user_id=sub,
        )
        session.add(user)
        session.flush()
    else:
        # Existing user signing in via Google — promote to verified if
        # Google says the email is verified. Doesn't rewrite the
        # original auth_provider.
        if email_verified and not user.email_verified:
            user.email_verified = True

    sess_row, sess_plain = _create_session(session, user)
    ws = session.get(Workspace, user.workspace_id)

    return {
        "user": _serialize_user(user),
        "workspace": _serialize_workspace(ws) if ws else None,
        "session_token": sess_plain,
        "session_expires_at": sess_row.expires_at.isoformat(),
        "is_new_user": is_new_user,
        "redirect_after": redirect_after,
    }


@app.get("/auth/me")
def auth_me(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    ws = session.get(Workspace, auth.workspace_id)
    return {
        "user": _serialize_user(auth.user) if auth.user else None,
        "workspace": _serialize_workspace(ws) if ws else None,
        "credential": "session" if auth.user else "api_key",
    }


def _serialize_session(s: SessionRow, current: bool) -> dict[str, Any]:
    return {
        "id": s.id,
        "created_at": s.created_at.isoformat(),
        "expires_at": s.expires_at.isoformat(),
        "revoked_at": s.revoked_at.isoformat() if s.revoked_at else None,
        "current": current,
    }


@app.get("/auth/sessions")
def list_sessions(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if auth.user is None:
        raise HTTPException(
            status_code=400, detail="sessions are only visible to a logged-in user"
        )
    rows = session.execute(
        select(SessionRow)
        .where(SessionRow.user_id == auth.user.id)
        .order_by(desc(SessionRow.created_at))
    ).scalars().all()
    current_id = auth.session.id if auth.session else None
    return {
        "sessions": [
            _serialize_session(s, current=s.id == current_id) for s in rows
        ]
    }


@app.delete("/auth/sessions/{session_id}")
def revoke_session(
    session_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if auth.user is None:
        raise HTTPException(status_code=400, detail="must be logged in")
    row = session.get(SessionRow, session_id)
    if row is None or row.user_id != auth.user.id:
        raise HTTPException(status_code=404, detail="session not found")
    if auth.session and row.id == auth.session.id:
        raise HTTPException(
            status_code=400, detail="cannot revoke the session used for this request"
        )
    if row.revoked_at is None:
        row.revoked_at = utcnow()
    session.flush()
    return _serialize_session(row, current=False)


# ---------- Phase 17.4: Stripe billing endpoints ---------- #


@app.post("/workspaces/me/billing/checkout")
def billing_create_checkout(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.4: create a Stripe Checkout session for the workspace.

    Lazy customer creation: most workspaces never upgrade, so we only
    call stripe.Customer.create the first time the user clicks
    "upgrade." Subsequent clicks reuse the stored customer_id.

    Returns `{checkout_url}`. The dashboard navigates the browser to it;
    on success Stripe redirects back to /account?upgrade=success, and
    the dashboard polls /auth/me until plan_tier flips to 'paid' (the
    webhook handler below lands within seconds).
    """
    import stripe_billing as _sb

    if not _sb.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Billing is not configured on this backend. Set "
                "LIGHTSEI_STRIPE_SECRET_KEY + LIGHTSEI_STRIPE_PRICE_ID."
            ),
        )
    if auth.user is None:
        raise HTTPException(
            status_code=400,
            detail="billing requires a logged-in user (not an API key)",
        )

    ws = session.get(Workspace, auth.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if ws.plan_tier == "paid":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "already_paid",
                "message": "Workspace already has an active subscription. Use /workspaces/me/billing/portal to manage it.",
            },
        )

    try:
        if not ws.stripe_customer_id:
            ws.stripe_customer_id = _sb.create_customer(
                email=auth.user.email, workspace_id=ws.id,
            )
            session.flush()
        result = _sb.create_checkout_session(
            customer_id=ws.stripe_customer_id, workspace_id=ws.id,
        )
    except _sb.StripeNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except _sb.StripeApiError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "stripe_error",
                "message": "Billing is temporarily unavailable. Try again in a moment.",
                "_debug": str(exc),
            },
        )

    return {"checkout_url": result["url"], "session_id": result["id"]}


@app.post("/workspaces/me/billing/portal")
def billing_create_portal(
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 17.4: create a Stripe Customer Portal session.

    Used by /account's "manage subscription" button on paid workspaces.
    400 if the workspace has never been to Checkout (no stripe_customer_id)
    so the dashboard can show "upgrade first" instead of a broken link.
    """
    import stripe_billing as _sb

    if not _sb.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Billing is not configured on this backend. Set "
                "LIGHTSEI_STRIPE_SECRET_KEY + LIGHTSEI_STRIPE_PRICE_ID."
            ),
        )
    if auth.user is None:
        raise HTTPException(
            status_code=400,
            detail="billing requires a logged-in user (not an API key)",
        )

    ws = session.get(Workspace, auth.workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    if not ws.stripe_customer_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_customer",
                "message": "Workspace has no billing history yet. Upgrade first.",
            },
        )

    try:
        result = _sb.create_portal_session(customer_id=ws.stripe_customer_id)
    except _sb.StripeNotConfiguredError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except _sb.StripeApiError as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "error": "stripe_error",
                "message": "Billing is temporarily unavailable. Try again in a moment.",
                "_debug": str(exc),
            },
        )

    return {"portal_url": result["url"], "session_id": result["id"]}


# Subscription lifecycle events we react to. Any other event types
# Stripe sends are acknowledged with 200 + ignored — we don't want
# Stripe retrying forever for events we genuinely don't care about.
_HANDLED_WEBHOOK_EVENTS = {
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.payment_failed",
}


@app.post("/billing/stripe/webhook")
async def stripe_webhook(request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    """Phase 17.4: Stripe webhook handler.

    Verifies the Stripe-Signature header against the configured signing
    secret, then flips plan_tier on the affected workspace based on the
    event type.

    Returns 200 on success (Stripe stops retrying). 400 on bad signature
    or unknown workspace (also stops retries). 5xx is reserved for
    transient infra failures where we WANT Stripe to retry.

    Idempotency: Stripe can deliver the same event twice. We re-derive
    plan_tier from the event's subscription status on every delivery,
    so duplicate delivery is a no-op (writes the same value twice).
    """
    import stripe_billing as _sb

    if not _sb.is_webhook_configured():
        # Shouldn't happen in a properly-configured deployment, but if
        # it does, log loud + return 400 so Stripe doesn't pile up
        # retries against a misconfigured endpoint.
        import logging as _logging
        _logging.getLogger("lightsei.billing").warning(
            "stripe webhook hit but LIGHTSEI_STRIPE_WEBHOOK_SECRET is not set"
        )
        raise HTTPException(
            status_code=400, detail="webhook secret not configured"
        )

    payload = await request.body()
    signature = request.headers.get("stripe-signature", "")

    try:
        event = _sb.construct_webhook_event(
            payload=payload, signature_header=signature,
        )
    except _sb.WebhookSignatureError as exc:
        raise HTTPException(status_code=400, detail=f"bad signature: {exc}")

    event_type = event.get("type") or ""
    if event_type not in _HANDLED_WEBHOOK_EVENTS:
        # Acknowledge + ignore. Common case for events we subscribe to
        # implicitly via the endpoint config (Stripe lets you select
        # specific events, but if the user picks "all events" we don't
        # want to 4xx the ones we don't care about).
        return {"status": "ignored", "type": event_type}

    obj = (event.get("data") or {}).get("object") or {}

    # Pull workspace_id off the event. checkout.session.completed has it
    # in client_reference_id; subscription events carry it in metadata
    # because we copied it onto subscription_data.metadata at checkout
    # creation time.
    workspace_id = (
        obj.get("client_reference_id")
        or (obj.get("metadata") or {}).get("workspace_id")
    )
    if not workspace_id:
        # Fallback: look up by stripe_customer_id. Covers older
        # subscriptions created before we started stamping the metadata.
        customer_id = obj.get("customer")
        if customer_id:
            ws = session.execute(
                select(Workspace).where(Workspace.stripe_customer_id == customer_id)
            ).scalar_one_or_none()
            if ws is not None:
                workspace_id = ws.id

    if not workspace_id:
        import logging as _logging
        _logging.getLogger("lightsei.billing").warning(
            "stripe webhook %s: no workspace_id in event %s",
            event_type, event.get("id"),
        )
        # Return 200 so Stripe stops retrying; the event is for a
        # workspace we don't recognize (likely a test event or a
        # workspace deleted on our side after subscription creation).
        return {"status": "ignored", "reason": "unknown_workspace"}

    ws = session.get(Workspace, workspace_id)
    if ws is None:
        return {"status": "ignored", "reason": "workspace_not_found"}

    if event_type == "checkout.session.completed":
        # Subscription is freshly created. The subscription id is on
        # the checkout session.
        sub_id = obj.get("subscription")
        if sub_id:
            ws.stripe_subscription_id = sub_id
        ws.plan_tier = "paid"
    elif event_type in ("customer.subscription.created", "customer.subscription.updated"):
        # Set plan_tier based on the subscription's current status.
        # 'active' + 'trialing' are paid; everything else (past_due,
        # unpaid, canceled, incomplete) downgrades to free so the
        # paywall starts firing again until they fix payment.
        status = obj.get("status") or ""
        ws.stripe_subscription_id = obj.get("id") or ws.stripe_subscription_id
        ws.plan_tier = "paid" if status in ("active", "trialing") else "free"
    elif event_type == "customer.subscription.deleted":
        # Subscription cancelled (either by the user via the Portal or
        # by Stripe after exhausted dunning). Drop to free; they keep
        # whatever free credits remain.
        ws.plan_tier = "free"
        ws.stripe_subscription_id = None
    elif event_type == "invoice.payment_failed":
        # First failure: log it but don't change plan_tier yet —
        # subscription.updated with status='past_due' will follow and
        # do the downgrade. This branch is mostly here for telemetry.
        import logging as _logging
        _logging.getLogger("lightsei.billing").warning(
            "stripe webhook: invoice payment failed for workspace %s",
            workspace_id,
        )

    session.flush()
    return {"status": "ok", "type": event_type, "workspace_id": workspace_id}


# ---------- Phase 19.2: Slack OAuth ---------- #


# How long the state row in slack_oauth_pending_states is valid. Has to
# cover the user's hop out to Slack's consent screen + back. 10 minutes
# is generous; reduce later if abandoned-flow rows pile up.
SLACK_OAUTH_STATE_TTL = timedelta(minutes=10)


@app.get("/slack/oauth/start")
def slack_oauth_start(
    redirect_after: Optional[str] = None,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 19.2: kick off the Slack app install flow.

    Returns `{authorization_url, state}` — the dashboard navigates the
    browser to authorization_url. State is persisted to
    slack_oauth_pending_states with the Lightsei workspace id + the
    operator's user id so the callback can rebind to the right
    workspace on return.

    503 if the Slack OAuth client isn't configured (env vars not set);
    fail loud rather than redirecting the user into a half-configured
    flow that errors out on Slack's end.

    `redirect_after` is where the dashboard wants the user to land
    post-install (defaults to /integrations/slack?installed=true in
    the callback).
    """
    import slack_oauth as _so

    if not _so.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Slack OAuth is not configured on this backend. Set "
                "LIGHTSEI_SLACK_CLIENT_ID + LIGHTSEI_SLACK_CLIENT_SECRET."
            ),
        )
    if auth.user is None:
        # Slack install is always operator-driven; can't be initiated
        # by an API-key-only context because we need to track who
        # clicked install on the audit trail.
        raise HTTPException(
            status_code=400,
            detail="Slack install must be initiated by a logged-in user",
        )

    now = utcnow()
    state = _so.new_state()

    session.add(SlackOAuthPendingState(
        state=state,
        lightsei_workspace_id=auth.workspace_id,
        installed_by_user_id=auth.user.id,
        redirect_after=redirect_after,
        created_at=now,
        expires_at=now + SLACK_OAUTH_STATE_TTL,
    ))
    session.flush()

    return {
        "authorization_url": _so.build_authorization_url(state=state),
        "state": state,
    }


@app.get("/slack/oauth/callback")
def slack_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: Session = Depends(get_session),
) -> Any:
    """Phase 19.2: complete the Slack install.

    Slack redirects the user's browser here after the consent screen.
    We validate the state, exchange the code for a bot token, encrypt
    + persist the bot token in slack_workspaces, then redirect the
    browser to the dashboard's /integrations/slack page.

    Error paths render a small HTML body the user can read and act on,
    not a JSON 4xx, because the user is in a browser tab — this is the
    one Lightsei endpoint that's expected to render directly to a
    person rather than feed a JS fetch.
    """
    import slack_oauth as _so
    import secrets_crypto

    dashboard_base = os.environ.get(
        "LIGHTSEI_DASHBOARD_BASE_URL", "https://app.lightsei.com"
    ).rstrip("/")

    def _html_error(title: str, message: str) -> Response:
        body = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Slack install — Lightsei</title>"
            "<style>body{font:14px/1.5 -apple-system,sans-serif;"
            "max-width:480px;margin:64px auto;padding:0 16px;color:#111}"
            "h1{font-size:18px;font-weight:600;margin-bottom:8px}"
            "p{color:#555;margin:8px 0}a{color:#4f46e5}</style></head>"
            f"<body><h1>{title}</h1><p>{message}</p>"
            f"<p><a href=\"{dashboard_base}/integrations\">"
            "← back to integrations</a></p></body></html>"
        )
        return Response(content=body, media_type="text/html", status_code=400)

    if error:
        # Slack hands back ?error=access_denied if the user cancelled
        # on the consent screen.
        return _html_error(
            "Slack install was cancelled",
            f"Slack returned an error: {error}. You can try again from "
            "the integrations page.",
        )

    if not code or not state:
        return _html_error(
            "Invalid Slack install link",
            "The Slack install callback was missing required parameters. "
            "Start the install again from the integrations page.",
        )

    pending = session.get(SlackOAuthPendingState, state)
    now = utcnow()
    if pending is None or pending.expires_at <= now:
        return _html_error(
            "Slack install link expired",
            "The install link is no longer valid (expired or already used). "
            "Start a fresh install from the integrations page.",
        )

    workspace_id = pending.lightsei_workspace_id
    installed_by_user_id = pending.installed_by_user_id
    redirect_after = pending.redirect_after
    # Single-use: drop the pending row before doing the exchange so a
    # parallel callback with the same state can't double-consume.
    session.delete(pending)
    session.flush()

    try:
        claims = _so.exchange_code_for_token(code=code)
    except _so.SlackOAuthError as exc:
        return _html_error(
            "Slack install didn't complete",
            f"The token exchange with Slack failed: {exc}. Try installing again.",
        )

    # Encrypt + store. The bot token is xoxb-... — sensitive enough that
    # we never log it; secrets_crypto.encrypt mirrors the workspace-
    # secret encryption path. Store as ASCII bytes of the base64 blob
    # so the LargeBinary column doesn't need a schema change.
    encrypted_blob = secrets_crypto.encrypt(claims["access_token"])
    token_bytes = encrypted_blob.encode("ascii")

    # If this Slack workspace was previously installed (and not revoked),
    # update the existing row in place rather than inserting a new one —
    # the partial-unique index would otherwise reject the new row.
    existing = session.get(SlackWorkspace, claims["team_id"])
    if existing is not None:
        existing.lightsei_workspace_id = workspace_id
        existing.team_name = claims["team_name"]
        existing.bot_token_encrypted = token_bytes
        existing.bot_user_id = claims["bot_user_id"]
        existing.installed_by_user_id = installed_by_user_id
        existing.installed_at = now
        existing.revoked_at = None
    else:
        session.add(SlackWorkspace(
            slack_team_id=claims["team_id"],
            lightsei_workspace_id=workspace_id,
            team_name=claims["team_name"],
            bot_token_encrypted=token_bytes,
            bot_user_id=claims["bot_user_id"],
            installed_by_user_id=installed_by_user_id,
            installed_at=now,
        ))
    session.flush()

    target = redirect_after or f"{dashboard_base}/integrations/slack?installed=true"
    # 303 makes the browser switch from GET to GET (which is what we
    # want — Slack's redirect was a GET; we're handing off to the
    # dashboard which is also GET).
    return RedirectResponse(target, status_code=303)


# ---------- Phase 19.3: Slack events webhook ---------- #


# Event types we route into the generation_jobs queue. Anything else
# is acknowledged with 200 + ignored so Slack stops retrying — the
# events webhook config in 19.7 will request only the events we
# handle, but Slack sometimes sends events we didn't subscribe to
# (e.g. when the app is added to a new channel).
_HANDLED_SLACK_EVENT_TYPES = {
    "app_mention",
}


@app.post("/slack/events")
async def slack_events(
    request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """Phase 19.3: Slack events webhook.

    Slack POSTs every subscribed event here (mentions, slash commands,
    membership changes). We:

    1. Echo the URL-verification challenge so Slack accepts the endpoint
       at configuration time.
    2. Verify the X-Slack-Signature HMAC + timestamp window so attackers
       can't forge events.
    3. Check idempotency against slack_events: Slack retries delivery up
       to 3 times within an hour; we treat a duplicate event_id as
       already-handled.
    4. Route app_mention events onto the generation_jobs queue as kind
       'slack_orchestration'. The 19.4 chat orchestrator handler picks
       them up + decides which bot responds.
    5. Acknowledge anything else with 200 + ignored.

    Returns 400 (not 5xx) on bad signature or missing config so Slack
    stops retrying a permanently-broken delivery.
    """
    import slack_events as _se

    payload_bytes = await request.body()

    # ---- Pre-parse for URL verification ---- #
    # Slack pings the endpoint at config time with a JSON body of
    # {type: "url_verification", challenge: "..."}. The signature is
    # included on this request too, but we want to handle the handshake
    # even on a fresh deployment where the signing secret might not
    # yet be in env. So: try to parse first; if it's a URL verification
    # and there's no challenge to verify against, just echo.
    try:
        body_json = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        # Not JSON; reject. Slack only ever sends JSON.
        raise HTTPException(status_code=400, detail="payload is not valid JSON")

    if body_json.get("type") == "url_verification":
        # Echo the challenge so Slack accepts the endpoint. Slack still
        # signs this request, but the URL-verification handshake is
        # what registers the endpoint in the first place, so we don't
        # gate it on signing-secret config.
        challenge = body_json.get("challenge")
        if not isinstance(challenge, str):
            raise HTTPException(
                status_code=400,
                detail="url_verification missing challenge",
            )
        return {"challenge": challenge}

    # ---- Signature verification ---- #
    if not _se.is_signing_configured():
        import logging as _logging
        _logging.getLogger("lightsei.slack_events").warning(
            "slack events webhook hit but LIGHTSEI_SLACK_SIGNING_SECRET is not set"
        )
        raise HTTPException(
            status_code=400, detail="signing secret not configured"
        )

    try:
        _se.verify_signature(
            body=payload_bytes,
            timestamp_header=request.headers.get("x-slack-request-timestamp"),
            signature_header=request.headers.get("x-slack-signature"),
        )
    except _se.SlackSignatureError as exc:
        raise HTTPException(status_code=400, detail=f"bad signature: {exc}")

    # ---- Idempotency check ---- #
    event = body_json.get("event") or {}
    event_id = body_json.get("event_id")
    slack_team_id = body_json.get("team_id") or body_json.get("api_app_id")
    if not event_id or not slack_team_id:
        # Malformed envelope. Still 200 + ignore so Slack doesn't retry.
        return {"status": "ignored", "reason": "missing_event_id_or_team_id"}

    # Try to insert the idempotency row first. If it's a duplicate,
    # IntegrityError signals we've already handled this; return 200 +
    # no-op so Slack stops retrying.
    from sqlalchemy.exc import IntegrityError
    now = utcnow()
    try:
        session.add(SlackEvent(
            slack_team_id=slack_team_id,
            event_id=event_id,
            kind=event.get("type") or "unknown",
            received_at=now,
        ))
        session.flush()
    except IntegrityError:
        session.rollback()
        return {"status": "duplicate", "event_id": event_id}

    # ---- Event routing ---- #
    event_type = event.get("type") or ""
    if event_type not in _HANDLED_SLACK_EVENT_TYPES:
        # Acknowledge + ignore. The Slack-app config in 19.7 will
        # subscribe only to events we handle, but Slack still sends
        # some events implicitly (membership_changed, etc.).
        return {"status": "ignored", "type": event_type}

    # Look up the Lightsei workspace from the slack_team_id so the
    # orchestrator job runs against the right workspace.
    sw = session.get(SlackWorkspace, slack_team_id)
    if sw is None or sw.revoked_at is not None:
        # Slack workspace isn't connected to a Lightsei workspace
        # (revoked or never installed). Drop the event silently.
        return {"status": "ignored", "reason": "slack_workspace_not_installed"}

    # Queue an orchestration job. The 19.4 handler reads (slack_team_id,
    # channel_id, user_id, text, thread_ts?) from the payload + decides
    # which bot responds.
    from jobs import enqueue_job
    job_id = str(uuid.uuid4())
    enqueue_job(
        session,
        job_id=job_id,
        workspace_id=sw.lightsei_workspace_id,
        kind="slack_orchestration",
        request_payload={
            "slack_team_id": slack_team_id,
            "channel_id": event.get("channel"),
            "user_id": event.get("user"),
            "text": event.get("text") or "",
            "thread_ts": event.get("thread_ts"),
            "ts": event.get("ts"),
            "slack_event_id": event_id,
        },
    )

    return {
        "status": "queued",
        "type": event_type,
        "job_id": job_id,
    }


# ---------- Phase 20.2: Google OAuth (connector install) ---------- #


CONNECTOR_OAUTH_STATE_TTL = timedelta(minutes=10)


@app.get("/connectors/google/start")
def connector_google_oauth_start(
    type: str,
    redirect_after: Optional[str] = None,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Phase 20.2: kick off the Google OAuth install for a connector.

    `type` is the connector name (e.g. `gmail`, `google_calendar`,
    `google_drive`). The scopes Lightsei requests come from the
    connector's `default_scopes` in CONNECTOR_REGISTRY.

    Returns `{authorization_url, state}` — the dashboard navigates the
    browser to authorization_url. State is persisted with the
    Lightsei workspace + operator + connector_type so the callback
    can rebind on return.
    """
    from connectors import get_connector
    from connectors import google_oauth as _gco

    spec = get_connector(type)
    if spec is None or spec.oauth_provider != "google":
        raise HTTPException(
            status_code=404,
            detail=f"unknown google connector {type!r}",
        )
    if not _gco.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "Google OAuth is not configured on this backend. Set "
                "LIGHTSEI_GOOGLE_CLIENT_ID + LIGHTSEI_GOOGLE_CLIENT_SECRET."
            ),
        )
    if auth.user is None:
        raise HTTPException(
            status_code=400,
            detail="connector install must be initiated by a logged-in user",
        )

    now = utcnow()
    verifier, challenge = _gco.new_pkce_pair()
    state = _gco.new_state()

    session.add(ConnectorOAuthPendingState(
        state=state,
        workspace_id=auth.workspace_id,
        installed_by_user_id=auth.user.id,
        connector_type=type,
        code_verifier=verifier,
        redirect_after=redirect_after,
        created_at=now,
        expires_at=now + CONNECTOR_OAUTH_STATE_TTL,
    ))
    session.flush()

    return {
        "authorization_url": _gco.build_authorization_url(
            state=state,
            challenge=challenge,
            scopes=list(spec.default_scopes),
        ),
        "state": state,
    }


@app.get("/connectors/google/callback")
def connector_google_oauth_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    session: Session = Depends(get_session),
) -> Any:
    """Phase 20.2: complete the connector install.

    Validates state, single-use'd (drops the pending row before
    exchange), trades the code for tokens, encrypts the {access_token,
    refresh_token, expires_at} blob, persists a ConnectorInstallation
    row (or updates an existing one in place for re-install), redirects
    the browser back to the dashboard.

    Renders HTML 400s on error paths so a browser tab gets a useful
    page rather than JSON (same pattern as 19.2's Slack callback).
    """
    import json as _json
    import secrets_crypto
    from connectors import get_connector
    from connectors import google_oauth as _gco

    dashboard_base = os.environ.get(
        "LIGHTSEI_DASHBOARD_BASE_URL", "https://app.lightsei.com"
    ).rstrip("/")

    def _html_error(title: str, message: str) -> Response:
        body = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>Connector install — Lightsei</title>"
            "<style>body{font:14px/1.5 -apple-system,sans-serif;"
            "max-width:480px;margin:64px auto;padding:0 16px;color:#111}"
            "h1{font-size:18px;font-weight:600;margin-bottom:8px}"
            "p{color:#555;margin:8px 0}a{color:#4f46e5}</style></head>"
            f"<body><h1>{title}</h1><p>{message}</p>"
            f"<p><a href=\"{dashboard_base}/integrations\">"
            "← back to integrations</a></p></body></html>"
        )
        return Response(content=body, media_type="text/html", status_code=400)

    if error:
        return _html_error(
            "Connector install cancelled",
            f"Google returned an error: {error}. You can try again from "
            "the integrations page.",
        )

    if not code or not state:
        return _html_error(
            "Invalid install link",
            "The Google install callback was missing required parameters. "
            "Start the install again from the integrations page.",
        )

    pending = session.get(ConnectorOAuthPendingState, state)
    now = utcnow()
    if pending is None or pending.expires_at <= now:
        return _html_error(
            "Install link expired",
            "The install link is no longer valid (expired or already used). "
            "Start a fresh install from the integrations page.",
        )

    workspace_id = pending.workspace_id
    installed_by_user_id = pending.installed_by_user_id
    connector_type = pending.connector_type
    verifier = pending.code_verifier
    redirect_after = pending.redirect_after
    # Single-use: drop the pending row before exchange so a parallel
    # callback with the same state can't double-consume.
    session.delete(pending)
    session.flush()

    spec = get_connector(connector_type)
    if spec is None or spec.oauth_provider != "google":
        return _html_error(
            "Unknown connector",
            f"The connector {connector_type!r} is no longer recognized.",
        )

    try:
        tokens = _gco.exchange_code_for_tokens(
            code=code, code_verifier=verifier,
        )
    except _gco.GoogleConnectorOAuthError as exc:
        return _html_error(
            "Install didn't complete",
            f"The token exchange with Google failed: {exc}. Try again "
            "from the integrations page.",
        )

    # Encrypt the token blob. We serialize a JSON dict so the refresh
    # path (20.6) can read+write the same shape, and `expires_at` is
    # carried as an ISO timestamp rather than seconds-from-now so we
    # don't have to plumb the install timestamp through every refresh.
    expires_at = None
    if tokens.get("expires_in") is not None:
        expires_at = (now + timedelta(seconds=int(tokens["expires_in"]))).isoformat()
    token_blob = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": expires_at,
    }
    encrypted = secrets_crypto.encrypt(_json.dumps(token_blob)).encode("ascii")

    # Granted scopes are a subset of what we asked for (user can
    # decline). Store what they actually granted.
    granted_scopes = (tokens.get("scope") or "").split() if tokens.get("scope") else []
    email = tokens.get("email")

    # Reinstall path: existing active install for (workspace, type)
    # gets updated in place so the partial-unique index doesn't fire.
    existing = session.execute(
        select(ConnectorInstallation).where(
            ConnectorInstallation.workspace_id == workspace_id,
            ConnectorInstallation.connector_type == connector_type,
            ConnectorInstallation.revoked_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        existing.encrypted_tokens = encrypted
        existing.scopes = granted_scopes
        existing.installed_by_user_id = installed_by_user_id
        existing.external_account_email = email or existing.external_account_email
        existing.installed_at = now
    else:
        session.add(ConnectorInstallation(
            id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            connector_type=connector_type,
            encrypted_tokens=encrypted,
            scopes=granted_scopes,
            installed_by_user_id=installed_by_user_id,
            external_account_email=email,
            installed_at=now,
        ))
    session.flush()

    target = redirect_after or f"{dashboard_base}/integrations?installed={connector_type}"
    return RedirectResponse(target, status_code=303)


# ---------- Phase 19.6: operator-facing Slack config endpoints ---------- #


def _serialize_slack_workspace(sw: SlackWorkspace) -> dict[str, Any]:
    """Surface the bits the dashboard's /integrations/slack page needs.
    Bot token is NEVER returned — that stays encrypted in storage."""
    return {
        "slack_team_id": sw.slack_team_id,
        "team_name": sw.team_name,
        "bot_user_id": sw.bot_user_id,
        "installed_at": sw.installed_at.isoformat(),
        "installed_by_user_id": sw.installed_by_user_id,
        "revoked_at": sw.revoked_at.isoformat() if sw.revoked_at else None,
    }


def _serialize_slack_channel(ch: SlackChannel) -> dict[str, Any]:
    return {
        "slack_team_id": ch.slack_team_id,
        "channel_id": ch.channel_id,
        "channel_name": ch.channel_name,
        "sensitivity_level": ch.sensitivity_level,
        "opted_in": ch.opted_in,
        "created_at": ch.created_at.isoformat(),
        "updated_at": ch.updated_at.isoformat(),
    }


@app.get("/workspaces/me/slack/workspaces")
def list_slack_workspaces(
    include_revoked: bool = False,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """List Slack workspaces connected to this Lightsei workspace.

    By default returns only active installs. Set `include_revoked=true`
    to see historical installs (useful for an audit log surface).
    """
    q = select(SlackWorkspace).where(
        SlackWorkspace.lightsei_workspace_id == auth.workspace_id
    )
    if not include_revoked:
        q = q.where(SlackWorkspace.revoked_at.is_(None))
    q = q.order_by(SlackWorkspace.installed_at.desc())
    rows = session.execute(q).scalars().all()
    return {"workspaces": [_serialize_slack_workspace(r) for r in rows]}


@app.get("/workspaces/me/slack/channels")
def list_slack_channels(
    slack_team_id: Optional[str] = None,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """List channels the Lightsei bot has been seen in.

    Filter by `slack_team_id` when the dashboard's already grouped by
    workspace; otherwise returns all channels across all installs
    for this Lightsei workspace. Ordered by (opted_in DESC, channel_name)
    so opted-in channels surface first — they're the actionable ones.
    """
    q = select(SlackChannel).where(
        SlackChannel.lightsei_workspace_id == auth.workspace_id
    )
    if slack_team_id:
        q = q.where(SlackChannel.slack_team_id == slack_team_id)
    q = q.order_by(SlackChannel.opted_in.desc(), SlackChannel.channel_name)
    rows = session.execute(q).scalars().all()
    return {"channels": [_serialize_slack_channel(r) for r in rows]}


class SlackChannelPatchIn(BaseModel):
    """Both fields optional — operator can flip either independently."""
    sensitivity_level: Optional[str] = Field(default=None, max_length=16)
    opted_in: Optional[bool] = None


@app.patch("/workspaces/me/slack/channels/{slack_team_id}/{channel_id}")
def patch_slack_channel(
    slack_team_id: str,
    channel_id: str,
    body: SlackChannelPatchIn,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Set the channel's `sensitivity_level` and/or `opted_in`. The
    chat orchestrator (Phase 19.4) reads these on every event.

    404 if the channel doesn't belong to this Lightsei workspace.
    422 if sensitivity_level isn't one of the four valid values.
    """
    from models import is_valid_sensitivity_level

    channel = session.get(SlackChannel, (slack_team_id, channel_id))
    if channel is None or channel.lightsei_workspace_id != auth.workspace_id:
        # Treat cross-workspace as 404 so the existence of a channel
        # in another tenant doesn't leak through the error code.
        raise HTTPException(
            status_code=404,
            detail=f"channel {slack_team_id!r}/{channel_id!r} not found",
        )

    if body.sensitivity_level is not None:
        if not is_valid_sensitivity_level(body.sensitivity_level):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"sensitivity_level {body.sensitivity_level!r} not in "
                    "{public, internal, sensitive, pii}"
                ),
            )
        channel.sensitivity_level = body.sensitivity_level

    if body.opted_in is not None:
        channel.opted_in = body.opted_in

    channel.updated_at = utcnow()
    session.flush()
    return _serialize_slack_channel(channel)


@app.delete("/workspaces/me/slack/workspaces/{slack_team_id}")
def revoke_slack_workspace(
    slack_team_id: str,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Revoke a Slack install.

    Sets `revoked_at` on the slack_workspaces row + best-effort calls
    Slack's `auth.revoke` to invalidate the stored bot token upstream.
    The row stays in the DB for audit; the partial-unique index from
    19.1 lets the same Slack workspace re-install after this without
    a manual cleanup.

    404 if the workspace install doesn't belong to this Lightsei
    workspace.
    """
    sw = session.get(SlackWorkspace, slack_team_id)
    if sw is None or sw.lightsei_workspace_id != auth.workspace_id:
        raise HTTPException(
            status_code=404,
            detail=f"slack workspace {slack_team_id!r} not found",
        )

    now = utcnow()
    if sw.revoked_at is None:
        sw.revoked_at = now
        session.flush()
        # Best-effort upstream revoke. Slack returns ok:true on success;
        # we don't fail the local revoke if Slack rejects (the token
        # might already be invalidated; the local revoke is what matters
        # for routing). Logged for ops.
        try:
            import secrets_crypto
            token = secrets_crypto.decrypt(sw.bot_token_encrypted.decode("ascii"))
            httpx_post = __import__("httpx").post
            r = httpx_post(
                "https://slack.com/api/auth.revoke",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
            if r.status_code >= 400 or not r.json().get("ok"):
                import logging as _logging
                _logging.getLogger("lightsei.slack").warning(
                    "auth.revoke not ok for %s: %s %s",
                    slack_team_id, r.status_code, r.text[:200],
                )
        except Exception as e:
            import logging as _logging
            _logging.getLogger("lightsei.slack").warning(
                "auth.revoke upstream call failed for %s: %s",
                slack_team_id, e,
            )

    return _serialize_slack_workspace(sw)


# ---------- Phase 19.5: agent-side Slack response endpoint ---------- #


class SlackRespondIn(BaseModel):
    """Input to `POST /slack/respond` (Phase 19.5).

    Called from bot code via `lightsei.post_slack(channel, text, ...)`.
    `source_agent` identifies the bot making the call so the capability
    gate can check its allow-list."""
    source_agent: str = Field(min_length=1, max_length=128)
    channel: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=40_000)
    thread_ts: Optional[str] = Field(default=None, max_length=64)


@app.post("/slack/respond")
def slack_respond(
    body: SlackRespondIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 19.5: agent-side helper for posting a message to Slack.

    Capability-gated on `slack:respond`. The agent's bot token is
    never exposed to bot code — the backend resolves the workspace's
    Slack install, decrypts the stored bot token, and calls
    chat.postMessage on the agent's behalf.

    Returns the Slack response on success. 4xx surfaces include:
    - 403 cross_zone_blocked-shape detail when the agent doesn't have
      the slack:respond capability granted.
    - 400 when the workspace has no active Slack install (the
      operator hasn't connected Slack yet).
    - 502 with a generic "Slack post failed" when Slack rejects the
      message (channel not found, bot not in channel, etc.).
    """
    import slack_client

    # Capability gate. Same shape as the cross-zone block surface from
    # /agents/{name}/commands so SDK code can map both 403s to the
    # typed LightseiCapabilityError without parsing free-form strings.
    agent = session.get(Agent, (workspace_id, body.source_agent))
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent {body.source_agent!r} not found in this workspace",
        )
    if "slack:respond" not in (agent.capabilities or []):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "capability_missing",
                "capability": "slack:respond",
                "agent_name": body.source_agent,
                "granted": list(agent.capabilities or []),
                "message": (
                    f"agent {body.source_agent!r} does not have the "
                    "'slack:respond' capability. Add it via "
                    "PATCH /agents/{name}/capabilities or set the "
                    "Compliance preset's internal / public hint."
                ),
            },
        )

    # Find the active Slack install for this Lightsei workspace. There
    # can be at most one non-revoked install (partial-unique index).
    active_install = session.execute(
        select(SlackWorkspace).where(
            SlackWorkspace.lightsei_workspace_id == workspace_id,
            SlackWorkspace.revoked_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if active_install is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "no_slack_install",
                "message": (
                    "No active Slack install for this workspace. Connect "
                    "Slack from /integrations/slack first."
                ),
            },
        )

    try:
        result = slack_client.post_message(
            session=session,
            slack_team_id=active_install.slack_team_id,
            channel=body.channel,
            text=body.text,
            thread_ts=body.thread_ts,
        )
    except slack_client.SlackClientError as exc:
        # Slack rejected the post (bot not in channel, channel_not_found,
        # etc.) OR the token is borked. Map to 502 with a clean message;
        # the raw error goes into _debug for ops.
        raise HTTPException(
            status_code=502,
            detail={
                "error": "slack_post_failed",
                "message": "Slack post failed — see _debug for the upstream error.",
                "_debug": str(exc),
            },
        )

    # Return the bits a bot might care about (ts for threading, channel
    # echo for paranoia). Drop everything else from Slack's response
    # to keep the surface stable across Slack API changes.
    return {
        "ok": True,
        "ts": result.get("ts"),
        "channel": result.get("channel"),
    }


# ---------- Phase 20.6: bot-callable connector endpoint ---------- #


class ConnectorInvokeIn(BaseModel):
    """Input to `POST /connectors/{type}/{tool}` (Phase 20.6).

    Called from bot code via `lightsei.gmail.send_email(...)` and
    friends (Phase 20.7 SDK helpers wrap this endpoint). `payload` is
    the tool's input dict; its shape is validated by the connector's
    own MANIFEST input_schema at the per-tool function level."""
    source_agent: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


@app.post("/connectors/{connector_type}/{tool_name}")
def invoke_connector(
    connector_type: str,
    tool_name: str,
    body: ConnectorInvokeIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Phase 20.6: bot-callable surface for installed connectors.

    Pipeline (and the rejection shape at each step):

    1. 404 `unknown_connector` if `connector_type` isn't in the
       registry.
    2. 404 if `source_agent` isn't an agent in this workspace.
    3. 403 `capability_missing` if the agent's `capabilities` list
       doesn't include `connector:{type}`. Same shape as
       `/slack/respond`'s 403 so SDK code can map both to
       `LightseiCapabilityError` without parsing strings.
    4. 403 `connector_zone_mismatch` if the agent's
       `sensitivity_level` isn't in the connector spec's
       `declared_zones`. The wedge invariant — e.g. a public-zoned
       bot can never touch Gmail because Gmail's declared_zones
       excludes public.
    5. 400 `connector_not_installed` if the workspace has no active
       install for `connector_type`. Operator hasn't run the
       `/connectors/google/start` flow yet.
    6. Decrypt the stored token blob, dispatch INVOKE.
    7. On `ConnectorAuthExpired` (401 from upstream): refresh the
       access_token via the connector's OAuth helper, persist the new
       encrypted blob, retry the INVOKE once. A second 401 surfaces
       as 502 `connector_auth_failed` — the install needs operator
       attention (refresh_token revoked, scopes changed upstream).
    8. On `ConnectorCallError`: 502 `connector_call_failed` with the
       upstream status in `_debug`.
    9. Record the call on `runs` (+ a single
       `connector_call_completed` event row) so the dashboard can
       answer "what is this bot actually doing in prod?" without an
       Anthropic-cost rollup needing to exist.
    """
    import json as _json

    import secrets_crypto
    from connectors import (
        CONNECTOR_REGISTRY,
        ConnectorAuthExpired,
        ConnectorCallError,
    )
    from connectors import google_oauth as _gco

    # 1. Registry lookup.
    spec = CONNECTOR_REGISTRY.get(connector_type)
    if spec is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "unknown_connector",
                "connector_type": connector_type,
                "message": (
                    f"no connector registered with type "
                    f"{connector_type!r}. Known: "
                    f"{sorted(CONNECTOR_REGISTRY.keys())}."
                ),
            },
        )

    # 2. Agent lookup.
    agent = session.get(Agent, (workspace_id, body.source_agent))
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail=f"agent {body.source_agent!r} not found in this workspace",
        )

    # 3. Capability gate — same shape as /slack/respond's 403.
    required_capability = f"connector:{connector_type}"
    if required_capability not in (agent.capabilities or []):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "capability_missing",
                "capability": required_capability,
                "agent_name": body.source_agent,
                "granted": list(agent.capabilities or []),
                "message": (
                    f"agent {body.source_agent!r} does not have the "
                    f"{required_capability!r} capability. Add it via "
                    "PATCH /agents/{name}/capabilities."
                ),
            },
        )

    # 4. Zone gate. declared_zones is a frozenset of sensitivity-level
    # strings. A mismatch means the connector flat-out refuses bots
    # at this trust zone (the wedge invariant).
    if agent.sensitivity_level not in spec.declared_zones:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "connector_zone_mismatch",
                "connector_type": connector_type,
                "agent_name": body.source_agent,
                "agent_sensitivity_level": agent.sensitivity_level,
                "declared_zones": sorted(spec.declared_zones),
                "message": (
                    f"connector {connector_type!r} refuses calls from "
                    f"{agent.sensitivity_level!r}-zoned bots. Declared "
                    f"zones: {sorted(spec.declared_zones)}."
                ),
            },
        )

    # 5. Install lookup. Partial-unique index guarantees at most one
    # non-revoked install per (workspace, type).
    install = session.execute(
        select(ConnectorInstallation).where(
            ConnectorInstallation.workspace_id == workspace_id,
            ConnectorInstallation.connector_type == connector_type,
            ConnectorInstallation.revoked_at.is_(None),
        ).limit(1)
    ).scalar_one_or_none()
    if install is None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "connector_not_installed",
                "connector_type": connector_type,
                "message": (
                    f"no active {connector_type!r} install for this "
                    "workspace. Connect it from /integrations first."
                ),
            },
        )

    # Decrypt the {access_token, refresh_token, expires_at} blob.
    try:
        token_blob = _json.loads(
            secrets_crypto.decrypt(bytes(install.encrypted_tokens))
        )
    except Exception:
        # Crypto failure — install is unrecoverable from here.
        # Surface as 500 since it indicates corruption / misconfig,
        # not a bot-correctable error.
        logger.exception("connector %s install %s token decrypt failed",
                         connector_type, install.id)
        raise HTTPException(
            status_code=500,
            detail={
                "error": "connector_token_decrypt_failed",
                "connector_type": connector_type,
                "message": (
                    "stored connector tokens could not be decrypted. "
                    "Reinstall the connector from /integrations."
                ),
            },
        )

    access_token = token_blob.get("access_token")
    refresh_token = token_blob.get("refresh_token")

    # 6 + 7. Dispatch, with a single refresh-then-retry on auth-expired.
    now = utcnow()
    invoke_error: Optional[str] = None
    upstream_status: Optional[int] = None
    try:
        try:
            result = spec.invoke(
                tool_name=tool_name,
                payload=body.payload,
                access_token=access_token,
            )
        except ConnectorAuthExpired:
            # Try one refresh + retry. If we have no refresh_token
            # (shouldn't happen — the install path requires it) or the
            # refresh itself fails, the install is dead.
            if not refresh_token:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "connector_auth_failed",
                        "connector_type": connector_type,
                        "message": (
                            "access token expired and no refresh "
                            "token on file. Reinstall the connector."
                        ),
                    },
                )
            try:
                refreshed = _gco.refresh_access_token(
                    refresh_token=refresh_token,
                )
            except _gco.GoogleConnectorOAuthError as exc:
                # invalid_grant etc. — install dead.
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "connector_auth_failed",
                        "connector_type": connector_type,
                        "message": (
                            "token refresh failed; reinstall the "
                            "connector from /integrations."
                        ),
                        "_debug": str(exc),
                    },
                )

            new_access = refreshed["access_token"]
            new_refresh = refreshed.get("refresh_token") or refresh_token
            new_expires_at = None
            if refreshed.get("expires_in") is not None:
                new_expires_at = (
                    now + timedelta(seconds=int(refreshed["expires_in"]))
                ).isoformat()
            new_blob = {
                "access_token": new_access,
                "refresh_token": new_refresh,
                "expires_at": new_expires_at,
            }
            install.encrypted_tokens = secrets_crypto.encrypt(
                _json.dumps(new_blob)
            ).encode("ascii")
            session.flush()

            # Retry once with the fresh access_token. A second 401
            # here means the upstream API is rejecting a fresh token
            # — install is dead from the user's side.
            try:
                result = spec.invoke(
                    tool_name=tool_name,
                    payload=body.payload,
                    access_token=new_access,
                )
            except ConnectorAuthExpired:
                raise HTTPException(
                    status_code=502,
                    detail={
                        "error": "connector_auth_failed",
                        "connector_type": connector_type,
                        "message": (
                            "upstream rejected a freshly-refreshed "
                            "token. Reinstall the connector."
                        ),
                    },
                )
    except ConnectorCallError as exc:
        upstream_status = exc.upstream_status
        invoke_error = str(exc)
        _record_connector_call(
            session,
            workspace_id=workspace_id,
            agent=agent,
            connector_type=connector_type,
            tool_name=tool_name,
            now=now,
            ok=False,
            error=invoke_error,
            upstream_status=upstream_status,
        )
        # Commit the recording before raising — get_session rolls
        # back on exception, which would lose the failure row. Any
        # other pending work (the refreshed encrypted_tokens blob, if
        # we got that far) is also worth persisting.
        session.commit()
        raise HTTPException(
            status_code=502,
            detail={
                "error": "connector_call_failed",
                "connector_type": connector_type,
                "tool_name": tool_name,
                "message": (
                    f"upstream {connector_type} API call failed — see "
                    "_debug for details."
                ),
                "_debug": {
                    "upstream_status": upstream_status,
                    "error": invoke_error,
                },
            },
        )

    # 9. Record the successful call.
    _record_connector_call(
        session,
        workspace_id=workspace_id,
        agent=agent,
        connector_type=connector_type,
        tool_name=tool_name,
        now=now,
        ok=True,
        error=None,
        upstream_status=None,
    )

    return {"ok": True, "result": result}


def _record_connector_call(
    session: Session,
    *,
    workspace_id: str,
    agent: "Agent",
    connector_type: str,
    tool_name: str,
    now: datetime,
    ok: bool,
    error: Optional[str],
    upstream_status: Optional[int],
) -> None:
    """Drop a Run + a single `connector_call_completed` event row so
    the dashboard's per-agent activity views (Phase 11.4 + later) can
    count connector calls alongside Anthropic Runs. cost_usd is zero
    — connector calls don't burn LLM tokens — but `sensitivity_level`
    is snapshotted from the agent so zone-rollup queries stay
    historically correct even if the agent's level changes later.
    """
    run_id = str(uuid.uuid4())
    session.add(Run(
        id=run_id,
        workspace_id=workspace_id,
        agent_name=agent.name,
        started_at=now,
        ended_at=now,
        sensitivity_level=agent.sensitivity_level,
    ))
    session.flush()
    event_payload: dict[str, Any] = {
        "connector_type": connector_type,
        "tool_name": tool_name,
        "ok": ok,
    }
    if error is not None:
        event_payload["error"] = error
    if upstream_status is not None:
        event_payload["upstream_status"] = upstream_status
    session.add(Event(
        workspace_id=workspace_id,
        run_id=run_id,
        agent_name=agent.name,
        kind="connector_call_completed" if ok else "connector_call_failed",
        payload=event_payload,
        timestamp=now,
    ))
    session.flush()


def _serialize_command(c: Command) -> dict[str, Any]:
    return {
        "id": c.id,
        "agent_name": c.agent_name,
        "kind": c.kind,
        "payload": c.payload or {},
        "status": c.status,
        "result": c.result,
        "error": c.error,
        "created_at": c.created_at.isoformat(),
        "claimed_at": c.claimed_at.isoformat() if c.claimed_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        "expires_at": c.expires_at.isoformat(),
        # Phase 11.2 dispatch chain fields.
        "source_agent": c.source_agent,
        "dispatch_chain_id": c.dispatch_chain_id,
        "dispatch_depth": c.dispatch_depth,
        "approval_state": c.approval_state,
        "approved_by_user_id": c.approved_by_user_id,
        "approved_at": c.approved_at.isoformat() if c.approved_at else None,
    }


def _resolve_auto_approval(
    session: Session,
    workspace_id: str,
    source_agent: Optional[str],
    target_agent: str,
    command_kind: str,
) -> str:
    """Lookup the approval mode for a (source, target, kind) tuple.

    Precedence:
        1. Exact match on all three.
        2. Wildcard source ('*' source + exact target + exact kind).
        3. Wildcard kind (exact source + exact target + '*' kind).
        4. No match — return 'pending', the default human-in-the-loop.

    `mode='auto_approve'` returns 'auto_approved'; 'require_human'
    returns 'pending' (explicit deny path so a wildcard can be
    overridden by a more specific require_human entry).

    When source_agent is None (user-initiated enqueue from the
    dashboard, or off-platform integration like /webhooks/github),
    skip rule matching entirely — the command goes straight to
    'auto_approved' since the user is already trusted.
    """
    if source_agent is None:
        return "auto_approved"
    candidates = [
        (source_agent, target_agent, command_kind),
        ("*", target_agent, command_kind),
        (source_agent, target_agent, "*"),
    ]
    for src, tgt, kind in candidates:
        rule = session.execute(
            select(CommandAutoApprovalRule).where(
                CommandAutoApprovalRule.workspace_id == workspace_id,
                CommandAutoApprovalRule.source_agent == src,
                CommandAutoApprovalRule.target_agent == tgt,
                CommandAutoApprovalRule.command_kind == kind,
            )
        ).scalar_one_or_none()
        if rule is None:
            continue
        return "auto_approved" if rule.mode == "auto_approve" else "pending"
    return "pending"


@app.post("/agents/{agent_name}/commands")
def enqueue_command(
    agent_name: str,
    body: CommandEnqueueIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Enqueue a command for `agent_name`.

    Phase 11.2 changes the shape: `dispatch_chain_id` and
    `source_agent` on the body are honored; depth + per-day caps are
    enforced when source_agent is set; auto_approval rules pre-flip
    the approval gate where applicable.
    """
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)

    parent_depth = -1  # depth = parent_depth + 1, so root commands get 0
    chain_id = body.dispatch_chain_id

    # When source_agent is set, this is an agent-driven dispatch and
    # we enforce the depth + per-day caps on the source agent.
    if body.source_agent:
        # Auto-register the source agent on first dispatch — same
        # pattern as ensure_agent above for the target. Means a new
        # agent can dispatch its first command without a separate
        # "register me" round-trip.
        ensure_agent(session, workspace_id, body.source_agent, now)
        source = session.get(Agent, (workspace_id, body.source_agent))
        if source is None:
            # ensure_agent should have created it; this is paranoia.
            raise HTTPException(
                status_code=400,
                detail=f"unknown source_agent {body.source_agent!r}",
            )
        # Look up the parent command in this chain (most recent for
        # this chain id) so we can compute depth = parent.depth + 1.
        if chain_id:
            parent = session.execute(
                select(Command)
                .where(
                    Command.workspace_id == workspace_id,
                    Command.dispatch_chain_id == chain_id,
                )
                .order_by(desc(Command.dispatch_depth))
                .limit(1)
            ).scalar_one_or_none()
            if parent is not None:
                parent_depth = parent.dispatch_depth
        depth = parent_depth + 1
        if depth >= source.max_dispatch_depth:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"dispatch chain depth {depth} would exceed "
                    f"{body.source_agent}'s max_dispatch_depth = "
                    f"{source.max_dispatch_depth}"
                ),
            )
        # Per-day rate cap: count commands the source has dispatched
        # in the last 24h. Filtered by source_agent so an agent's own
        # incoming commands don't count against its outgoing budget.
        cutoff = now - timedelta(hours=24)
        recent = session.execute(
            text(
                """
                SELECT COUNT(*) AS n FROM commands
                WHERE workspace_id = :wsid
                  AND source_agent = :src
                  AND created_at >= :cutoff
                """
            ),
            {"wsid": workspace_id, "src": body.source_agent, "cutoff": cutoff},
        ).first()
        if recent and (recent.n or 0) >= source.max_dispatch_per_day:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{body.source_agent} has dispatched {recent.n} "
                    f"commands in the last 24h, exceeding its "
                    f"max_dispatch_per_day = {source.max_dispatch_per_day}"
                ),
            )
        # Phase 16.4: cross-zone dispatch enforcement. The load-bearing
        # piece. Same sensitivity level always allowed; different
        # levels refused unless the source agent has
        # dispatches_cross_zone=True. Only applies when source_agent is
        # set (user-initiated dispatches via the dashboard skip this —
        # the user is making an explicit cross-zone decision). Auto-
        # approval rules from Phase 11.2 still apply on top.
        target = session.get(Agent, (workspace_id, agent_name))
        if (
            target is not None
            and source.sensitivity_level != target.sensitivity_level
            and not source.dispatches_cross_zone
        ):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "cross_zone_blocked",
                    "source_agent": body.source_agent,
                    "source_zone": source.sensitivity_level,
                    "target_agent": agent_name,
                    "target_zone": target.sensitivity_level,
                    "message": (
                        f"{body.source_agent!r} ({source.sensitivity_level!r}) "
                        f"cannot dispatch to {agent_name!r} "
                        f"({target.sensitivity_level!r}) — set "
                        f"dispatches_cross_zone=True on the source agent "
                        f"to permit cross-zone dispatches "
                        f"(auto-approval rules still apply on top)."
                    ),
                },
            )
    else:
        depth = 0  # user / off-platform enqueue is a chain root

    # Generate a fresh chain id when none was provided.
    if not chain_id:
        chain_id = str(uuid.uuid4())

    approval_state = _resolve_auto_approval(
        session,
        workspace_id=workspace_id,
        source_agent=body.source_agent,
        target_agent=agent_name,
        command_kind=body.kind,
    )

    cmd = Command(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind=body.kind,
        payload=body.payload,
        status="pending",
        source_agent=body.source_agent,
        dispatch_chain_id=chain_id,
        dispatch_depth=depth,
        approval_state=approval_state,
        approved_at=now if approval_state == "auto_approved" else None,
        created_at=now,
        expires_at=now + COMMAND_TTL,
    )
    session.add(cmd)
    session.flush()
    return _serialize_command(cmd)


@app.get("/agents/{agent_name}/commands")
def list_commands(
    agent_name: str,
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    rows = session.execute(
        select(Command)
        .where(
            Command.workspace_id == workspace_id,
            Command.agent_name == agent_name,
        )
        .order_by(desc(Command.created_at))
        .limit(limit)
    ).scalars().all()
    return {"commands": [_serialize_command(c) for c in rows]}


@app.post("/agents/{agent_name}/commands/claim")
def claim_command(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Atomically claim the oldest pending command for this agent.

    Phase 11.2: only commands with approval_state in
    {'approved', 'auto_approved'} are claimable. 'pending' commands
    sit safely until a human acts on them; 'rejected' / 'expired'
    are terminal.

    Uses Postgres `SELECT ... FOR UPDATE SKIP LOCKED` so two agents
    polling concurrently never claim the same command.
    """
    now = utcnow()
    row = session.execute(
        text(
            """
            SELECT id FROM commands
            WHERE workspace_id = :wsid
              AND agent_name = :agent
              AND status = 'pending'
              AND approval_state IN ('approved', 'auto_approved')
              AND expires_at > :now
            ORDER BY created_at ASC
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        ),
        {"wsid": workspace_id, "agent": agent_name, "now": now},
    ).first()
    if row is None:
        return {"command": None}
    cmd = session.get(Command, row.id)
    if cmd is None:
        return {"command": None}
    cmd.status = "claimed"
    cmd.claimed_at = now
    session.flush()
    return {"command": _serialize_command(cmd)}


# ---------- Phase 11.2: approval endpoints + auto-approval CRUD ---------- #


@app.post("/commands/{command_id}/approve")
def approve_command(
    command_id: str,
    body: CommandApprovalIn,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Flip a pending command to 'approved' so the next claim picks
    it up. Only works on commands in the caller's workspace and only
    when approval_state is currently 'pending' (idempotent flips
    would be confusing — a re-approve isn't meaningfully different
    from the first one)."""
    workspace_id = auth.workspace_id
    cmd = session.get(Command, command_id)
    if cmd is None or cmd.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.approval_state != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"command already {cmd.approval_state!r}",
        )
    cmd.approval_state = "approved"
    cmd.approved_by_user_id = auth.user.id if auth.user else None
    cmd.approved_at = utcnow()
    session.flush()
    return _serialize_command(cmd)


@app.post("/commands/{command_id}/reject")
def reject_command(
    command_id: str,
    body: CommandApprovalIn,
    auth: AuthResult = Depends(get_authenticated),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """Flip a pending command to 'rejected' (terminal). Records the
    user who rejected on approved_by_user_id so the audit trail is
    consistent — the column reflects "who acted on the gate," not
    specifically who said yes."""
    workspace_id = auth.workspace_id
    cmd = session.get(Command, command_id)
    if cmd is None or cmd.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.approval_state != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"command already {cmd.approval_state!r}",
        )
    cmd.approval_state = "rejected"
    cmd.status = "cancelled"
    cmd.approved_by_user_id = auth.user.id if auth.user else None
    cmd.approved_at = utcnow()
    cmd.completed_at = utcnow()
    if body.reason:
        cmd.error = f"rejected: {body.reason}"
    session.flush()
    return _serialize_command(cmd)


def _serialize_auto_approval_rule(r: CommandAutoApprovalRule) -> dict[str, Any]:
    return {
        "source_agent": r.source_agent,
        "target_agent": r.target_agent,
        "command_kind": r.command_kind,
        "mode": r.mode,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


@app.get("/workspaces/me/auto-approval-rules")
def list_auto_approval_rules(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(CommandAutoApprovalRule)
        .where(CommandAutoApprovalRule.workspace_id == workspace_id)
        .order_by(
            CommandAutoApprovalRule.source_agent,
            CommandAutoApprovalRule.target_agent,
            CommandAutoApprovalRule.command_kind,
        )
    ).scalars().all()
    return {"rules": [_serialize_auto_approval_rule(r) for r in rows]}


@app.put("/workspaces/me/auto-approval-rules")
def upsert_auto_approval_rule(
    body: AutoApprovalRuleIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Upsert by composite PK so the same call creates a new rule or
    updates an existing one. PK is (workspace, source, target, kind),
    which means flipping a rule from 'auto_approve' to 'require_human'
    on the same triple lands as a single update."""
    now = utcnow()
    existing = session.get(
        CommandAutoApprovalRule,
        (workspace_id, body.source_agent, body.target_agent, body.command_kind),
    )
    if existing is None:
        rule = CommandAutoApprovalRule(
            workspace_id=workspace_id,
            source_agent=body.source_agent,
            target_agent=body.target_agent,
            command_kind=body.command_kind,
            mode=body.mode,
            created_at=now,
            updated_at=now,
        )
        session.add(rule)
    else:
        existing.mode = body.mode
        existing.updated_at = now
        rule = existing
    session.flush()
    return _serialize_auto_approval_rule(rule)


@app.delete("/workspaces/me/auto-approval-rules")
def delete_auto_approval_rule(
    source_agent: str,
    target_agent: str,
    command_kind: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, str]:
    rule = session.get(
        CommandAutoApprovalRule,
        (workspace_id, source_agent, target_agent, command_kind),
    )
    if rule is None:
        raise HTTPException(status_code=404, detail="rule not found")
    session.delete(rule)
    session.flush()
    return {"status": "ok"}


# ---------- Phase 11.6: dispatch-chain views ---------- #


def _aggregate_chain_status(commands: list[Command]) -> str:
    """Roll up a chain's overall status from its commands.

    Priority (most "interesting" wins so the user sees actionable state
    first): pending_approval > running > failed > expired > done.
    `pending_approval` is split out from generic `pending` because the
    UI surfaces a click-to-approve button on those rows; the user's
    glance-and-skip pattern wants that signal at the top of the heap.
    """
    states = {c.status for c in commands}
    approval_states = {c.approval_state for c in commands}
    if "pending" in approval_states:
        return "pending_approval"
    if "running" in states or "claimed" in states:
        return "running"
    if "failed" in states:
        return "failed"
    if "expired" in states:
        return "expired"
    if "rejected" in approval_states:
        return "rejected"
    # Command.status uses "completed" (set by complete_command).
    if all(c.status == "completed" for c in commands):
        return "done"
    return "pending"


@app.get("/workspaces/me/dispatch")
def list_dispatch_chains(
    limit: int = 50,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """List dispatch chains, newest first.

    A "chain" is the set of commands sharing one `dispatch_chain_id` —
    the cause-and-effect tree that starts with a user click, scheduled
    tick, or webhook push, and fans out through agent-to-agent
    dispatches. The 11.6 dashboard view renders one row per chain,
    expandable into a timeline that this endpoint's siblings populate.
    """
    limit = max(1, min(limit, 200))
    # Find the most recently active chain ids for this workspace.
    chain_rows = session.execute(
        text(
            """
            SELECT dispatch_chain_id AS chain_id,
                   MIN(created_at)   AS started_at,
                   MAX(COALESCE(completed_at, claimed_at, created_at))
                                     AS last_activity_at
            FROM commands
            WHERE workspace_id = :wsid
            GROUP BY dispatch_chain_id
            ORDER BY last_activity_at DESC
            LIMIT :lim
            """
        ),
        {"wsid": workspace_id, "lim": limit},
    ).all()

    if not chain_rows:
        return {"chains": []}

    chain_ids = [r.chain_id for r in chain_rows]
    cmds = session.execute(
        select(Command)
        .where(
            Command.workspace_id == workspace_id,
            Command.dispatch_chain_id.in_(chain_ids),
        )
        .order_by(Command.dispatch_depth, Command.created_at)
    ).scalars().all()

    by_chain: dict[str, list[Command]] = {}
    for c in cmds:
        by_chain.setdefault(c.dispatch_chain_id, []).append(c)

    chains: list[dict[str, Any]] = []
    for r in chain_rows:
        chain_cmds = by_chain.get(r.chain_id, [])
        if not chain_cmds:
            continue
        root = chain_cmds[0]  # ordered by depth ASC, so depth=0 first
        pending_approvals = sum(
            1 for c in chain_cmds if c.approval_state == "pending"
        )
        chains.append(
            {
                "chain_id": r.chain_id,
                "started_at": r.started_at.isoformat(),
                "last_activity_at": r.last_activity_at.isoformat(),
                "command_count": len(chain_cmds),
                "max_depth": max(c.dispatch_depth for c in chain_cmds),
                "root_agent": root.agent_name,
                "root_kind": root.kind,
                "root_source_agent": root.source_agent,
                "status": _aggregate_chain_status(chain_cmds),
                "pending_approval_count": pending_approvals,
            }
        )
    return {"chains": chains}


@app.get("/workspaces/me/dispatch/{chain_id}")
def get_dispatch_chain(
    chain_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Full timeline for one dispatch chain.

    Returns every command in the chain (with the same shape as
    `/agents/{name}/commands`) plus any events whose payload carries a
    `command_id` matching one of the chain's commands. The frontend
    renders these together as a single vertical timeline indented by
    `dispatch_depth`.
    """
    cmds = session.execute(
        select(Command)
        .where(
            Command.workspace_id == workspace_id,
            Command.dispatch_chain_id == chain_id,
        )
        .order_by(Command.dispatch_depth, Command.created_at)
    ).scalars().all()
    if not cmds:
        raise HTTPException(status_code=404, detail="chain not found")

    cmd_ids = [c.id for c in cmds]
    # Events linked to these commands via payload.command_id. Atlas /
    # Hermes / Polaris all stamp this; events from agents that don't
    # are excluded — for 11.6 that's the right call (the chain view
    # shouldn't pull in arbitrary agent telemetry).
    events = session.execute(
        text(
            """
            SELECT id, workspace_id, run_id, agent_name, kind, payload, timestamp
            FROM events
            WHERE workspace_id = :wsid
              AND payload->>'command_id' = ANY(:cmd_ids)
            ORDER BY timestamp ASC
            """
        ),
        {"wsid": workspace_id, "cmd_ids": cmd_ids},
    ).all()

    return {
        "chain_id": chain_id,
        "commands": [_serialize_command(c) for c in cmds],
        "events": [
            {
                "id": e.id,
                "run_id": e.run_id,
                "agent_name": e.agent_name,
                "kind": e.kind,
                "payload": e.payload or {},
                "timestamp": e.timestamp.isoformat(),
                "command_id": (e.payload or {}).get("command_id"),
            }
            for e in events
        ],
        "status": _aggregate_chain_status(cmds),
    }


@app.post("/commands/{command_id}/complete")
def complete_command(
    command_id: str,
    body: CommandCompleteIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    cmd = session.get(Command, command_id)
    if cmd is None or cmd.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.status not in ("claimed", "pending"):
        raise HTTPException(status_code=400, detail=f"command already {cmd.status}")
    if body.error:
        cmd.status = "failed"
        cmd.error = body.error
    else:
        cmd.status = "completed"
        cmd.result = body.result
    cmd.completed_at = utcnow()
    session.flush()
    return _serialize_command(cmd)


@app.delete("/commands/{command_id}")
def cancel_command(
    command_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    cmd = session.get(Command, command_id)
    if cmd is None or cmd.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="command not found")
    if cmd.status != "pending":
        raise HTTPException(
            status_code=400,
            detail=f"can only cancel pending commands; this one is {cmd.status}",
        )
    cmd.status = "cancelled"
    cmd.completed_at = utcnow()
    session.flush()
    return _serialize_command(cmd)


def _serialize_manifest(a: Agent) -> dict[str, Any]:
    return {
        "agent_name": a.name,
        "command_handlers": a.command_handlers or [],
        "last_seen_at": a.last_seen_at.isoformat() if a.last_seen_at else None,
    }


@app.put("/agents/{agent_name}/manifest")
def put_manifest(
    agent_name: str,
    body: AgentManifestIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Replace an agent's command-handler manifest. Called by the SDK on
    init() once handlers are known. Also bumps last_seen_at."""
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=500, detail="agent ensure failed")
    a.command_handlers = body.command_handlers
    a.last_seen_at = now
    a.updated_at = now
    session.flush()
    return _serialize_manifest(a)


@app.get("/agents/{agent_name}/manifest")
def get_manifest(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        return {
            "agent_name": agent_name,
            "command_handlers": [],
            "last_seen_at": None,
        }
    return _serialize_manifest(a)


# ---------- Anthropic error translation ---------- #
#
# Both 12B.1 (generate one bot) and 12C.1 (plan a team) hit the same
# Anthropic API with forced tool_choice. When Anthropic returns 529
# (overloaded) the user gets a confusing 502; translate to a clear 503
# with retry guidance instead. Other API errors surface as 502 like
# before.

def _anthropic_error_to_http(exc: "Exception") -> HTTPException:
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


# ---------- Phase 12B.1: agent code generation ---------- #


@app.post("/workspaces/me/agents/generate", status_code=202)
def generate_agent(
    body: AgentGenerateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Enqueue an agent-generation job and return its id.

    Phase 12C.6: the synchronous Anthropic call moved off the request
    path because it could exceed Railway's edge timeout (~100s with
    retries on Opus). The endpoint now validates input + pre-checks
    workspace state, then writes a `pending` row to `generation_jobs`
    and returns `{job_id, status: "pending"}`. The dashboard polls
    `GET /workspaces/me/generation-jobs/{id}` until terminal.

    Pre-checks that 4xx synchronously (not enqueued): no
    ANTHROPIC_API_KEY secret, or workspace already over its monthly
    budget. Anything that can only fail mid-LLM-call (rate limits,
    validation retry exhaustion, etc.) is recorded as `error` on the
    job row instead.
    """
    import jobs

    # 1. Auth-paired prereqs: workspace's Anthropic key + cost-cap room.
    secret_row = session.get(
        WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY")
    )
    if secret_row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ANTHROPIC_API_KEY not set on this workspace. Add it on "
                "/account before using the bot generator."
            ),
        )

    # Cost-cap check: workspace.budget_usd_monthly is the cap; reject
    # if MTD spend already at or over. Generation calls are LLM calls
    # like any other and shouldn't bypass the budget gate.
    workspace = session.get(Workspace, workspace_id)
    if workspace and workspace.budget_usd_monthly is not None:
        cost = workspace_cost_mtd(session, workspace_id)
        used = float(cost.get("total_usd") or 0)
        cap = float(workspace.budget_usd_monthly)
        if cap > 0 and used >= cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"workspace MTD spend ${used:.2f} ≥ budget ${cap:.2f}; "
                    "bump the budget on /account to keep generating."
                ),
            )

    job_id = str(uuid.uuid4())
    jobs.enqueue_job(
        session,
        job_id=job_id,
        workspace_id=workspace_id,
        kind="agent_generate",
        request_payload=body.model_dump(exclude_none=False),
    )
    return {"job_id": job_id, "status": "pending"}


# ---------- Phase 12C.1: project-analysis endpoint ---------- #


@app.post("/workspaces/me/teams/plan", status_code=202)
def plan_team(
    body: TeamPlanIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Enqueue a team-plan job and return its id.

    Phase 12C.6: the synchronous Anthropic call (and the GitHub README
    fetch) moved off the request path; both could push request time past
    Railway's ~100s edge timeout, especially when paired with a
    validation retry. The endpoint now validates input + pre-checks
    workspace state, then writes a `pending` row to `generation_jobs`
    and returns `{job_id, status: "pending"}`. The dashboard polls
    `GET /workspaces/me/generation-jobs/{id}` until terminal.

    Pre-checks that 4xx synchronously (not enqueued): missing project-info
    input, no ANTHROPIC_API_KEY secret, malformed github_repo, or
    workspace already over its monthly budget. Anything that can only
    fail mid-run (Anthropic errors, GitHub fetch failures, validation
    retry exhaustion) is recorded as `error` on the job row instead.
    """
    import jobs

    # 1. Input gate: at least one source of project info.
    if not (body.readme_text or body.freeform_description or body.github_repo):
        raise HTTPException(
            status_code=400,
            detail=(
                "Provide at least one of `readme_text`, "
                "`freeform_description`, or `github_repo`."
            ),
        )

    # 2. Workspace's Anthropic key gate (same shape as /agents/generate).
    secret_row = session.get(
        WorkspaceSecret, (workspace_id, "ANTHROPIC_API_KEY")
    )
    if secret_row is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "ANTHROPIC_API_KEY not set on this workspace. Add it on "
                "/account before using the team planner."
            ),
        )

    # 3. Budget gate.
    workspace = session.get(Workspace, workspace_id)
    if workspace and workspace.budget_usd_monthly is not None:
        cost = workspace_cost_mtd(session, workspace_id)
        used = float(cost.get("total_usd") or 0)
        cap = float(workspace.budget_usd_monthly)
        if cap > 0 and used >= cap:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"workspace MTD spend ${used:.2f} ≥ budget ${cap:.2f}; "
                    "bump the budget on /account to keep planning."
                ),
            )

    # 4. github_repo format validation: pre-parse so the user sees
    # malformed-URL errors synchronously. The actual GitHub fetch
    # happens in the handler so a slow network call is off the request
    # path. We stash the parsed pair on the payload so the handler
    # doesn't re-parse.
    payload = body.model_dump(exclude_none=False)
    if body.github_repo and not body.readme_text:
        owner, name = _parse_github_repo(body.github_repo)
        payload["github_repo_parsed"] = [owner, name]

    job_id = str(uuid.uuid4())
    jobs.enqueue_job(
        session,
        job_id=job_id,
        workspace_id=workspace_id,
        kind="team_plan",
        request_payload=payload,
    )
    return {"job_id": job_id, "status": "pending"}


# ---------- Phase 12C.6.5: poll endpoint for generation_jobs ---------- #


@app.get("/workspaces/me/generation-jobs/{job_id}")
def get_generation_job(
    job_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Return the current state of a generation job.

    Dashboard polls this until `status` is terminal (`success` or
    `failed`). 404 covers both "no such row" and "row belongs to a
    different workspace" — we don't leak existence across workspaces.
    """
    row = session.get(GenerationJob, job_id)
    if row is None or row.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "result_payload": row.result_payload,
        "error": row.error,
        "attempt_count": row.attempt_count,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "finished_at": row.finished_at.isoformat() if row.finished_at else None,
    }


def _parse_github_repo(s: str) -> tuple[str, str]:
    """Accept `owner/name` or a full GitHub URL and return `(owner, name)`."""
    raw = s.strip()
    # Tolerate trailing `.git` (common for clone URLs).
    if raw.endswith(".git"):
        raw = raw[:-4]
    # https://github.com/owner/name or git@github.com:owner/name
    for prefix in ("https://github.com/", "http://github.com/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
            break
    if raw.startswith("git@github.com:"):
        raw = raw[len("git@github.com:"):]
    parts = raw.strip("/").split("/")
    if len(parts) < 2:
        raise HTTPException(
            status_code=400,
            detail=f"github_repo must be `owner/name` or a GitHub URL (got {s!r})",
        )
    return parts[0], parts[1]


def _serialize_instance(i: AgentInstance, now: datetime) -> dict[str, Any]:
    age = now - i.last_heartbeat_at
    return {
        "id": i.id,
        "agent_name": i.agent_name,
        "hostname": i.hostname,
        "pid": i.pid,
        "sdk_version": i.sdk_version,
        "started_at": i.started_at.isoformat(),
        "last_heartbeat_at": i.last_heartbeat_at.isoformat(),
        "status": "active" if age <= INSTANCE_ACTIVE_WINDOW else "stale",
    }


@app.post("/agents/{agent_name}/instances/heartbeat")
def instance_heartbeat(
    agent_name: str,
    body: InstanceHeartbeatIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Upsert by instance_id: register on first call, refresh last_heartbeat_at
    on every subsequent call. Idempotent. SDK calls this on init() and then
    on a timer.
    """
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    inst = session.get(AgentInstance, body.instance_id)
    if inst is None:
        # Hostname-scoped concurrency cap: only enforced on new
        # registrations (existing instances refreshing their own
        # heartbeat are not subject to it — they were registered
        # under a previous-or-current cap). Stale rows from
        # crashed processes don't count because the window filter
        # excludes them.
        if body.hostname:
            cutoff = now - INSTANCE_ACTIVE_WINDOW
            active_count = session.execute(
                select(func.count(AgentInstance.id))
                .where(AgentInstance.workspace_id == workspace_id)
                .where(AgentInstance.agent_name == agent_name)
                .where(AgentInstance.hostname == body.hostname)
                .where(AgentInstance.last_heartbeat_at >= cutoff)
            ).scalar_one()
            if active_count >= MAX_INSTANCES_PER_HOSTNAME:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"refused: {active_count} active instance(s) of "
                        f"'{agent_name}' already running on '{body.hostname}' "
                        f"(cap = {MAX_INSTANCES_PER_HOSTNAME}). Kill the "
                        "older processes (e.g. `pkill -f bot.py`) before "
                        "starting another, or raise the cap via "
                        "LIGHTSEI_MAX_INSTANCES_PER_HOSTNAME."
                    ),
                )
        inst = AgentInstance(
            id=body.instance_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            hostname=body.hostname,
            pid=body.pid,
            sdk_version=body.sdk_version,
            started_at=body.started_at or now,
            last_heartbeat_at=now,
        )
        session.add(inst)
    elif inst.workspace_id != workspace_id or inst.agent_name != agent_name:
        # Instance id collision across workspaces or agents — refuse rather
        # than overwrite. SDK uses uuid4 so this is virtually impossible
        # without a deliberately spoofed id.
        raise HTTPException(
            status_code=409, detail="instance id belongs to another agent",
        )
    else:
        inst.last_heartbeat_at = now
        # Allow these to refresh in case the SDK updates between heartbeats.
        if body.hostname is not None:
            inst.hostname = body.hostname
        if body.pid is not None:
            inst.pid = body.pid
        if body.sdk_version is not None:
            inst.sdk_version = body.sdk_version
    session.flush()
    # Phase 16.3: echo the agent's current capability list back so the
    # SDK can refresh its cache on every heartbeat. Dashboard edits to
    # `capabilities` propagate within one heartbeat interval (default
    # 10s) — no separate fetch needed.
    response = _serialize_instance(inst, now)
    agent_row = session.get(Agent, (workspace_id, agent_name))
    if agent_row is not None:
        response["capabilities"] = list(agent_row.capabilities or [])
        response["sensitivity_level"] = agent_row.sensitivity_level
    return response


@app.get("/agents/{agent_name}/instances")
def list_instances(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """List instances for this agent, newest heartbeat first. Each row carries
    a computed `status` of `active` or `stale` so the dashboard doesn't need
    to know the heartbeat threshold."""
    now = utcnow()
    rows = session.execute(
        select(AgentInstance)
        .where(
            AgentInstance.workspace_id == workspace_id,
            AgentInstance.agent_name == agent_name,
        )
        .order_by(desc(AgentInstance.last_heartbeat_at))
    ).scalars().all()
    return {"instances": [_serialize_instance(i, now) for i in rows]}


def _serialize_thread(t: Thread) -> dict[str, Any]:
    return {
        "id": t.id,
        "agent_name": t.agent_name,
        "title": t.title,
        "created_at": t.created_at.isoformat(),
        "updated_at": t.updated_at.isoformat(),
    }


def _serialize_thread_message(m: ThreadMessage) -> dict[str, Any]:
    return {
        "id": m.id,
        "thread_id": m.thread_id,
        "role": m.role,
        "content": m.content,
        "status": m.status,
        "error": m.error,
        "created_at": m.created_at.isoformat(),
        "completed_at": m.completed_at.isoformat() if m.completed_at else None,
    }


def _thread_for_workspace(
    session: Session, thread_id: str, workspace_id: str
) -> Thread:
    t = session.get(Thread, thread_id)
    if t is None or t.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="thread not found")
    return t


@app.post("/agents/{agent_name}/threads")
def create_thread(
    agent_name: str,
    body: ThreadCreateIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    t = Thread(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        title=body.title or "New thread",
        created_at=now,
        updated_at=now,
    )
    session.add(t)
    session.flush()
    return _serialize_thread(t)


@app.get("/agents/{agent_name}/threads")
def list_threads(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(Thread)
        .where(
            Thread.workspace_id == workspace_id,
            Thread.agent_name == agent_name,
        )
        .order_by(desc(Thread.updated_at))
    ).scalars().all()
    return {"threads": [_serialize_thread(t) for t in rows]}


@app.get("/threads/{thread_id}")
def get_thread(
    thread_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    t = _thread_for_workspace(session, thread_id, workspace_id)
    msgs = session.execute(
        select(ThreadMessage)
        .where(ThreadMessage.thread_id == t.id)
        .order_by(ThreadMessage.created_at)
    ).scalars().all()
    return {
        "thread": _serialize_thread(t),
        "messages": [_serialize_thread_message(m) for m in msgs],
    }


@app.delete("/threads/{thread_id}")
def delete_thread(
    thread_id: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    t = _thread_for_workspace(session, thread_id, workspace_id)
    session.delete(t)
    session.flush()
    return {"deleted": thread_id}


@app.post("/threads/{thread_id}/messages")
def post_thread_message(
    thread_id: str,
    body: ThreadMessagePostIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """User sends a message. Creates a 'user' completed message and a
    'pending' assistant message that the agent will claim."""
    t = _thread_for_workspace(session, thread_id, workspace_id)
    now = utcnow()
    user_msg = ThreadMessage(
        id=str(uuid.uuid4()),
        thread_id=t.id,
        role="user",
        content=body.content,
        status="completed",
        created_at=now,
        completed_at=now,
    )
    pending = ThreadMessage(
        id=str(uuid.uuid4()),
        thread_id=t.id,
        role="assistant",
        content="",
        status="pending",
        created_at=now,
    )
    session.add(user_msg)
    session.add(pending)
    t.updated_at = now
    # If the thread still has the default title, derive one from the first
    # user message — capped at 60 chars.
    if t.title == "New thread":
        t.title = body.content.strip().splitlines()[0][:60]
    session.flush()
    return {
        "user_message": _serialize_thread_message(user_msg),
        "pending_message": _serialize_thread_message(pending),
    }


@app.post("/agents/{agent_name}/threads/claim")
def claim_thread_turn(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Atomically claim the oldest pending assistant message across all
    threads for this agent in the caller's workspace. Returns the message
    id plus the full conversation history (completed messages only)."""
    now = utcnow()
    row = session.execute(
        text(
            """
            SELECT m.id AS message_id, m.thread_id AS thread_id
            FROM thread_messages m
            JOIN threads t ON t.id = m.thread_id
            WHERE t.workspace_id = :wsid
              AND t.agent_name = :agent
              AND m.status = 'pending'
              AND m.role = 'assistant'
            ORDER BY m.created_at ASC
            LIMIT 1
            FOR UPDATE OF m SKIP LOCKED
            """
        ),
        {"wsid": workspace_id, "agent": agent_name},
    ).first()
    if row is None:
        return {"turn": None}
    msg = session.get(ThreadMessage, row.message_id)
    if msg is None:
        return {"turn": None}
    # Mark as claimed by writing claimed_at? We use status transitions: we
    # leave status='pending' (no separate claimed state for messages —
    # callers are expected to either complete or fail). To prevent re-claim
    # during the same instant, FOR UPDATE plus the next claim's WHERE
    # status='pending' is enough; but we also bump it to 'in_progress' for
    # clarity.
    msg.status = "in_progress"
    history = session.execute(
        select(ThreadMessage)
        .where(
            ThreadMessage.thread_id == row.thread_id,
            ThreadMessage.status == "completed",
        )
        .order_by(ThreadMessage.created_at)
    ).scalars().all()
    messages = [{"role": m.role, "content": m.content} for m in history]

    # Prepend the agent's configured system prompt if set and not already
    # present at the start of the thread.
    agent_row = session.get(Agent, (workspace_id, agent_name))
    if (
        agent_row is not None
        and agent_row.system_prompt
        and not (messages and messages[0].get("role") == "system")
    ):
        messages = [
            {"role": "system", "content": agent_row.system_prompt},
            *messages,
        ]

    session.flush()
    return {
        "turn": {
            "message_id": msg.id,
            "thread_id": msg.thread_id,
            "messages": messages,
        }
    }


@app.post("/messages/{message_id}/chunk")
def append_thread_message_chunk(
    message_id: str,
    body: ThreadMessageChunkIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    """Append streaming output to an in-progress assistant message."""
    msg = session.get(ThreadMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    t = session.get(Thread, msg.thread_id)
    if t is None or t.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="message not found")
    if msg.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=400, detail=f"message already {msg.status}"
        )
    if msg.status == "pending":
        msg.status = "in_progress"
    msg.content = (msg.content or "") + body.delta
    t.updated_at = utcnow()
    session.flush()
    return _serialize_thread_message(msg)


@app.post("/messages/{message_id}/complete")
def complete_thread_message(
    message_id: str,
    body: ThreadMessageCompleteIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    msg = session.get(ThreadMessage, message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    t = session.get(Thread, msg.thread_id)
    if t is None or t.workspace_id != workspace_id:
        raise HTTPException(status_code=404, detail="message not found")
    if msg.status not in ("pending", "in_progress"):
        raise HTTPException(
            status_code=400,
            detail=f"message already {msg.status}",
        )
    now = utcnow()
    if body.error:
        msg.status = "failed"
        msg.error = body.error
    else:
        msg.status = "completed"
        # If the caller passed content, that wins (final authoritative). If
        # not, keep whatever we accumulated from chunks.
        if body.content is not None:
            msg.content = body.content
    msg.completed_at = now
    t.updated_at = now
    session.flush()
    return _serialize_thread_message(msg)


@app.get("/agents/{agent_name}/cost")
def get_agent_cost(
    agent_name: str,
    since: Optional[datetime] = Query(
        None, description="ISO 8601 UTC. Defaults to start of today UTC."
    ),
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    since_ts = since or utc_day_start()
    summary = agent_cost_since(session, workspace_id, agent_name, since_ts)
    return {
        "agent_name": agent_name,
        "since": since_ts.isoformat(),
        "as_of": utcnow().isoformat(),
        **summary,
    }
