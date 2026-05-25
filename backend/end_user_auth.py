"""Phase 25.3: end-user bearer-token auth resolver.

Parallel module to `backend/auth.py` for the consumer-facing surface
spec'd in Phase 25. Resolves an `EndUserSession` from an
`Authorization: Bearer <token>` header and returns the linked
`EndUser` row + the workspaces they're subscribed to as a customer.

Two intentional design choices:

1. **Distinct module from `auth.py`.** Operator credentials
   (`Session` / `ApiKey`) and end-user credentials (`EndUserSession`)
   are different entity types resolving to different identities. A
   single resolver that tried to handle both would be a magnet for
   accidental privilege confusion. Keeping them apart means an
   endpoint that asks for `get_end_user` can never accidentally
   accept an operator session.

2. **Cross-token-type protection.** End-user sessions use the same
   `bks_` prefix + sha256-hex hash shape as operator sessions
   (both use `keys.generate_session_token`). That keeps the wire
   format uniform but means a hash lookup against just
   `EndUserSession` could let an operator session resolve in the
   wrong direction in some hypothetical future where someone
   forgets the type check. Defensive: if an unknown end-user
   token's hash matches a row in the operator `Session` table,
   we 401 with a distinctive detail string rather than the generic
   "invalid session" so a misconfigured endpoint surfaces
   immediately in tests + logs.

Endpoints that accept end-user auth pass through `get_end_user`
explicitly via FastAPI's `Depends`. Phase 25.4 wires the widget
endpoints to use this dep on the optional-auth path.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from db import get_session
from keys import hash_token
from models import (
    EndUser,
    EndUserSession,
    EndUserVendorLink,
    Session as SessionRow,
    Workspace,
)


@dataclass
class EndUserAuthResult:
    """What the resolver hands back to callers.

    `linked_workspaces` is the list of workspaces the end user is
    actively subscribed to (`end_user_vendor_links.removed_at IS NULL`).
    Phase 25.4 + 26 + 27 surfaces use this to scope what the end user
    can see across vendors without re-querying per request.
    """
    end_user: EndUser
    session: EndUserSession
    linked_workspaces: list[Workspace] = field(default_factory=list)


def _parse_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return None


def _resolve(
    authorization: Optional[str], session: Session,
) -> EndUserAuthResult:
    token = _parse_bearer(authorization)
    if token is None:
        raise HTTPException(
            status_code=401, detail="missing end-user session token",
        )

    h = hash_token(token)
    now = datetime.now(timezone.utc)

    eu_sess = session.execute(
        select(EndUserSession).where(EndUserSession.token_hash == h)
    ).scalar_one_or_none()

    if eu_sess is None:
        # Cross-token-type guard: if the hash IS in the operator
        # `sessions` table, the caller mixed up their bearer. Distinct
        # detail string so it surfaces in test failures + log queries.
        op_sess = session.execute(
            select(SessionRow).where(SessionRow.token_hash == h)
        ).scalar_one_or_none()
        if op_sess is not None:
            raise HTTPException(
                status_code=401,
                detail="operator session token not valid for end-user auth",
            )
        raise HTTPException(
            status_code=401, detail="invalid end-user session",
        )

    if eu_sess.revoked_at is not None:
        raise HTTPException(
            status_code=401, detail="end-user session revoked",
        )
    if eu_sess.expires_at <= now:
        raise HTTPException(
            status_code=401, detail="end-user session expired",
        )

    end_user = session.get(EndUser, eu_sess.end_user_id)
    if end_user is None:
        # Defensive: FK CASCADE should have killed the session row
        # alongside the end_user, but if a race left a dangling
        # session 401 cleanly instead of 500ing on a NoneType later.
        raise HTTPException(
            status_code=401, detail="invalid end-user session",
        )

    # Active subscriptions only: a soft-revoked link (removed_at set)
    # means the end user can read past conversations but isn't a
    # current customer of that vendor. Phase 27.2 wires the soft-
    # revoke flow; for now this filter is forward-compatible.
    linked_rows = session.execute(
        select(Workspace)
        .join(
            EndUserVendorLink,
            EndUserVendorLink.workspace_id == Workspace.id,
        )
        .where(
            EndUserVendorLink.end_user_id == end_user.id,
            EndUserVendorLink.removed_at.is_(None),
        )
    ).scalars().all()

    return EndUserAuthResult(
        end_user=end_user,
        session=eu_sess,
        linked_workspaces=list(linked_rows),
    )


def get_end_user(
    authorization: Optional[str] = Header(default=None),
    session: Session = Depends(get_session),
) -> EndUserAuthResult:
    """FastAPI dep for endpoints that accept end-user auth.

    401s on missing / invalid / expired / revoked / cross-type tokens.
    Returns the resolved EndUserAuthResult on success.
    """
    return _resolve(authorization, session)
