"""Phase 26.1: tests for the workspaces.vendor_slug column + claim
endpoint.

Two surfaces:

1. Schema: column exists nullable, uniqueness enforced at the DB
   layer.
2. `POST /workspaces/me/vendor-slug` + `GET /workspaces/me`
   extension: valid claim, format validation (parameterized over
   invalid shapes), collision returns 409, no-op on re-claim,
   workspace-scoped (operator A can't see operator B's slug).
3. Validation helper `is_valid_vendor_slug` directly: parameterized
   accept + reject cases.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import Workspace, is_valid_vendor_slug
from tests.conftest import auth_headers, signup


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------- Schema ---------- #


def test_vendor_slug_column_is_nullable_and_unique():
    """Two workspaces can coexist with NULL slugs (NULL doesn't
    conflict). A second workspace claiming the same non-null slug
    fails the unique constraint."""
    with session_scope() as s:
        s.add(Workspace(
            id=str(uuid.uuid4()), name="ws-a",
            created_at=_utcnow(), vendor_slug=None,
        ))
        s.add(Workspace(
            id=str(uuid.uuid4()), name="ws-b",
            created_at=_utcnow(), vendor_slug=None,
        ))
        s.add(Workspace(
            id=str(uuid.uuid4()), name="ws-c",
            created_at=_utcnow(), vendor_slug="acme",
        ))

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(Workspace(
            id=str(uuid.uuid4()),
            name="collision-co",
            created_at=_utcnow(),
            vendor_slug="acme",  # already taken
        ))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_vendor_slug_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'workspaces' "
            "AND indexname = 'ix_workspaces_vendor_slug'"
        )).first()
        assert r is not None


# ---------- Validator helper ---------- #


@pytest.mark.parametrize("slug", [
    "abc", "acme", "acme-corp", "halo-eu",
    "a1b", "a" * 32, "z-9",
    "jyni", "acme-customer-success",
])
def test_is_valid_vendor_slug_accepts(slug: str):
    assert is_valid_vendor_slug(slug)


@pytest.mark.parametrize("slug", [
    "ab",            # too short
    "a" * 33,        # too long
    "Acme",          # uppercase
    "ACME",
    "has space",
    "has_underscore",
    "-acme",         # leading dash
    "acme-",         # trailing dash
    "--",
    "a-",
    "-a",
    "a..b",
    "a@b",
    "",
    None,
    1,
])
def test_is_valid_vendor_slug_rejects(slug):
    assert not is_valid_vendor_slug(slug)


# ---------- POST /workspaces/me/vendor-slug ---------- #


def test_claim_vendor_slug_happy_path(client, alice):
    """Valid claim returns 200 + the serialized workspace with the
    new vendor_slug set."""
    r = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "alice-co"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == alice["workspace"]["id"]
    assert body["vendor_slug"] == "alice-co"

    # Persisted.
    with session_scope() as s:
        ws = s.get(Workspace, alice["workspace"]["id"])
        assert ws.vendor_slug == "alice-co"


def test_get_my_workspace_returns_vendor_slug(client, alice):
    """GET /workspaces/me echoes whatever vendor_slug is set
    (NULL pre-claim, the claimed value post-claim)."""
    # Pre-claim: NULL.
    r1 = client.get(
        "/workspaces/me",
        headers=auth_headers(alice["session_token"]),
    )
    assert r1.status_code == 200
    assert r1.json()["vendor_slug"] is None

    # Claim, then re-read.
    client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "alice-co"},
    )
    r2 = client.get(
        "/workspaces/me",
        headers=auth_headers(alice["session_token"]),
    )
    assert r2.json()["vendor_slug"] == "alice-co"


def test_claim_same_slug_twice_is_noop(client, alice):
    """Claiming the same slug for the same workspace twice returns
    200 both times (idempotent)."""
    r1 = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "alice-co"},
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "alice-co"},
    )
    assert r2.status_code == 200


def test_claim_different_slug_replaces(client, alice):
    """Operator can change their mind. New claim replaces the old."""
    client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "alice-co"},
    )
    r = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "alice-corp"},
    )
    assert r.status_code == 200
    assert r.json()["vendor_slug"] == "alice-corp"
    with session_scope() as s:
        ws = s.get(Workspace, alice["workspace"]["id"])
        assert ws.vendor_slug == "alice-corp"


def test_claim_taken_slug_returns_409(client, alice, bob):
    """Bob owns 'bob-co'; Alice tries to claim it = 409."""
    rb = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(bob["session_token"]),
        json={"slug": "bob-co"},
    )
    assert rb.status_code == 200

    ra = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "bob-co"},
    )
    assert ra.status_code == 409
    assert ra.json()["detail"]["error"] == "vendor_slug_taken"

    # Alice's workspace_slug unchanged (still NULL).
    with session_scope() as s:
        ws = s.get(Workspace, alice["workspace"]["id"])
        assert ws.vendor_slug is None


@pytest.mark.parametrize("bad_slug", [
    "ab",
    "Acme",
    "-acme",
    "acme-",
    "has_underscore",
    "has space",
])
def test_claim_invalid_format_returns_422(client, alice, bad_slug: str):
    r = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": bad_slug},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("bad_slug", ["", "a"])
def test_claim_too_short_rejected_by_pydantic(client, alice, bad_slug: str):
    """Pydantic min_length=3 catches the absurdly-short cases before
    the endpoint handler even runs. Both paths surface as 422."""
    r = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": bad_slug},
    )
    assert r.status_code == 422


def test_claim_too_long_rejected_by_pydantic(client, alice):
    """Pydantic max_length=32 catches it before is_valid_vendor_slug."""
    r = client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "a" * 33},
    )
    assert r.status_code == 422


def test_claim_workspace_scoped(client, alice, bob):
    """Alice claims 'alice-co'; Bob's GET /workspaces/me does NOT
    see it (each operator's session resolves their own workspace)."""
    client.post(
        "/workspaces/me/vendor-slug",
        headers=auth_headers(alice["session_token"]),
        json={"slug": "alice-co"},
    )
    rb = client.get(
        "/workspaces/me",
        headers=auth_headers(bob["session_token"]),
    )
    assert rb.json()["vendor_slug"] is None  # Bob has no slug yet
    assert rb.json()["id"] != alice["workspace"]["id"]


def test_claim_requires_auth(client):
    """No bearer = 401, same as every other operator endpoint."""
    r = client.post(
        "/workspaces/me/vendor-slug",
        json={"slug": "anon-claim"},
    )
    assert r.status_code == 401
