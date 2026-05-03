import hashlib
import hmac
import json
import os
import secrets as _stdlib_secrets
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
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
from db import ensure_agent, get_session, session_scope
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
    Event,
    EventValidation,
    GitHubAgentPath,
    GitHubIntegration,
    NotificationChannel,
    NotificationDelivery,
    Run,
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
    # Distinguish "field not provided" from "explicitly null". Pydantic v2:
    # we'll detect via model_fields_set.


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
    if event.kind == "llm_call_completed":
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
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


@app.get("/agents")
def list_agents(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(Agent).where(Agent.workspace_id == workspace_id).order_by(Agent.name)
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
    a.updated_at = now
    session.flush()
    return _serialize_agent(a)


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

    failed_validations = session.execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM event_validations ev
            JOIN events e ON e.id = ev.event_id
            WHERE e.workspace_id = :wsid
              AND ev.status = 'fail'
              AND ev.created_at >= :cutoff_24h
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

    stale_agents = session.execute(
        text(
            """
            SELECT COUNT(*) AS n
            FROM (
                SELECT a.name,
                       (SELECT MAX(ai.last_heartbeat_at)
                        FROM agent_instances ai
                        WHERE ai.workspace_id = :wsid
                          AND ai.agent_name = a.name) AS last_hb
                FROM agents a
                WHERE a.workspace_id = :wsid
            ) sub
            WHERE sub.last_hb IS NOT NULL
              AND sub.last_hb < :cutoff_5m
            """
        ),
        {"wsid": workspace_id, "cutoff_5m": cutoff_5m},
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
            ORDER BY a.name
            """
        ),
        {"wsid": workspace_id, "cutoff_24h": cutoff_24h},
    ).all()

    # Filter out agents that have never had any activity AND aren't
    # explicitly tagged as orchestrator — keeps the canvas free of
    # dormant placeholder rows.
    now = utcnow()
    agents_out: list[dict[str, Any]] = []
    for r in agent_rows:
        last_hb = r.last_heartbeat_at
        if last_hb is None and r.runs_24h == 0 and r.role != "orchestrator":
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

    return {
        "status": "ok",
        "event": "push",
        "ref": ref,
        "commit_sha": commit_sha,
        "queued_redeploys": queued,
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
    return _serialize_instance(inst, now)


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
