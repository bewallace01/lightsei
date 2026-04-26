import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

import policies
from auth import AuthResult, get_authenticated, get_workspace_id
from cost import agent_cost_since, utc_day_start
from db import ensure_agent, get_session
from keys import (
    generate_key,
    generate_session_token,
    hash_token,
    prefix_for_display,
)
from migrate import upgrade_to_head
from models import (
    Agent,
    ApiKey,
    Event,
    Run,
    Session as SessionRow,
    User,
    Workspace,
)
from passwords import hash_password, verify_password

SESSION_TTL = timedelta(days=30)


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


@app.on_event("startup")
def on_startup() -> None:
    upgrade_to_head()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events")
def post_event(
    event: EventIn,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    ts = event.timestamp or utcnow()
    ensure_agent(session, workspace_id, event.agent_name, ts)

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


@app.get("/agents")
def list_agents(
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    rows = session.execute(
        select(Agent).where(Agent.workspace_id == workspace_id).order_by(Agent.name)
    ).scalars().all()
    return {
        "agents": [
            {
                "name": a.name,
                "daily_cost_cap_usd": a.daily_cost_cap_usd,
                "created_at": a.created_at.isoformat(),
                "updated_at": a.updated_at.isoformat(),
            }
            for a in rows
        ]
    }


@app.get("/agents/{agent_name}")
def get_agent(
    agent_name: str,
    session: Session = Depends(get_session),
    workspace_id: str = Depends(get_workspace_id),
) -> dict[str, Any]:
    a = session.get(Agent, (workspace_id, agent_name))
    if a is None:
        raise HTTPException(status_code=404, detail="agent not found")
    return {
        "name": a.name,
        "daily_cost_cap_usd": a.daily_cost_cap_usd,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


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
    a.daily_cost_cap_usd = body.daily_cost_cap_usd
    a.updated_at = now
    session.flush()
    return {
        "name": a.name,
        "daily_cost_cap_usd": a.daily_cost_cap_usd,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
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
def signup(body: SignupIn, session: Session = Depends(get_session)) -> dict[str, Any]:
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
def login(body: LoginIn, session: Session = Depends(get_session)) -> dict[str, Any]:
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
