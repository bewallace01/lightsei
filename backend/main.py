import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, select, text
from sqlalchemy.orm import Session

import limits
import policies
import secrets_crypto
from auth import AuthResult, get_authenticated, get_workspace_id
from cost import agent_cost_since, utc_day_start
from db import ensure_agent, get_session
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
    Deployment,
    DeploymentBlob,
    DeploymentLog,
    Event,
    EventValidation,
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
import notifications
import validators
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
    name: str = Field(min_length=1)


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


@app.patch("/workspaces/me")
def patch_me(
    body: WorkspacePatchIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    ws = session.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=404, detail="workspace not found")
    ws.name = body.name
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
    }


@app.post("/agents/{agent_name}/commands")
def enqueue_command(
    agent_name: str,
    body: CommandEnqueueIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    now = utcnow()
    ensure_agent(session, workspace_id, agent_name, now)
    cmd = Command(
        id=str(uuid.uuid4()),
        workspace_id=workspace_id,
        agent_name=agent_name,
        kind=body.kind,
        payload=body.payload,
        status="pending",
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

    Uses Postgres `SELECT ... FOR UPDATE SKIP LOCKED` so two agents polling
    concurrently never claim the same command.
    """
    now = utcnow()
    row = session.execute(
        text(
            """
            SELECT id FROM commands
            WHERE workspace_id = :wsid
              AND agent_name = :agent
              AND status = 'pending'
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
