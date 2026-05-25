"""Bearer-token auth.

Two credential types live in the same `Authorization: Bearer <token>` header:
  - API keys (prefix `bk_`) authenticate as a workspace, no user identity.
    The workspace is `api_key.workspace_id` — pinned at key creation.
  - Session tokens (prefix `bks_`) authenticate as a user. Phase 23.2 onward,
    the workspace is `session.active_workspace_id` (the dashboard's per-session
    active pointer), guarded by a `workspace_members` lookup so a session
    whose user was removed from the active workspace (or whose workspace was
    deleted from another tab) 401s cleanly instead of silently leaking access.

In both cases the dep returns a workspace_id. `get_authenticated_request`
also returns whichever credential row was used so endpoints that need the
acting user (e.g. `/auth/me`) can ask for it.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_session
from keys import SESSION_PREFIX, hash_token, is_api_key, is_session_token
from models import ApiKey, Session as SessionRow, User, WorkspaceMember


@dataclass
class AuthResult:
    workspace_id: str
    api_key: Optional[ApiKey] = None  # set when bearer was an API key
    user: Optional[User] = None       # set when bearer was a session token
    session: Optional["SessionRow"] = None  # set when bearer was a session token


def _parse_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def _resolve(
    authorization: Optional[str], session: Session
) -> AuthResult:
    token = _parse_bearer(authorization)
    if token is None:
        raise HTTPException(status_code=401, detail="missing api key")

    h = hash_token(token)
    now = datetime.now(timezone.utc)

    if is_session_token(token):
        sess = session.execute(
            select(SessionRow).where(SessionRow.token_hash == h)
        ).scalar_one_or_none()
        if sess is None:
            raise HTTPException(status_code=401, detail="invalid session")
        if sess.revoked_at is not None:
            raise HTTPException(status_code=401, detail="session revoked")
        if sess.expires_at <= now:
            raise HTTPException(status_code=401, detail="session expired")
        user = session.get(User, sess.user_id)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid session")
        # Phase 23.2: the dashboard's active workspace lives on the
        # session row (per-session, so two tabs can hold different
        # workspaces open). A NULL active pointer means the workspace
        # was deleted from another tab (FK SET NULL fired) or the
        # session predates migration backfill — either way the
        # dashboard routes the user to the workspace picker.
        if sess.active_workspace_id is None:
            raise HTTPException(
                status_code=401, detail="no active workspace",
            )
        # Defensive: confirm the user is still a member of the active
        # workspace. Guards against a stale session whose user got
        # removed (future Phase 23B invite-revoke flow) or whose
        # active_workspace_id somehow points at a workspace the user
        # never joined.
        is_member = session.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == user.id,
                WorkspaceMember.workspace_id == sess.active_workspace_id,
            )
        ).scalar_one_or_none()
        if is_member is None:
            raise HTTPException(
                status_code=401, detail="not a member of active workspace",
            )
        return AuthResult(
            workspace_id=sess.active_workspace_id,
            user=user,
            session=sess,
        )

    # Anything else falls through to api_keys. Note we deliberately allow
    # tokens without the `bk_` prefix here so the seeded "demo-key" still
    # works.
    row = session.execute(
        select(ApiKey).where(ApiKey.hash == h)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=401, detail="invalid api key")
    if row.revoked_at is not None:
        raise HTTPException(status_code=401, detail="api key revoked")
    row.last_used_at = now
    return AuthResult(workspace_id=row.workspace_id, api_key=row)


def get_workspace_id(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> str:
    return _resolve(authorization, session).workspace_id


def get_authenticated(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> AuthResult:
    return _resolve(authorization, session)
