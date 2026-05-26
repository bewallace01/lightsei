"""Phase 27.1: schema tests for vendor_invite_codes + the two new
columns on end_user_vendor_links.

Surfaces:

1. `vendor_invite_codes` table: roundtrip, code uniqueness (it's the
   PK so a duplicate insert fails), workspace FK cascade,
   consumed_by_end_user_id SET NULL on end-user delete (audit row
   survives), index landing.
2. `end_user_vendor_links.display_name_override`: nullable column,
   roundtrip stores + reads, defaults to NULL.
3. `end_user_vendor_links.notification_pref`: server default 'all',
   accepts known values, validator helper rejects unknowns.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from db import session_scope
from models import (
    DEFAULT_NOTIFICATION_PREF,
    EndUser,
    EndUserVendorLink,
    VendorInviteCode,
    Workspace,
    _VALID_NOTIFICATION_PREFS,
    is_valid_notification_pref,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    ws_id = str(uuid.uuid4())
    s.add(Workspace(
        id=ws_id, name=f"ws-{ws_id[:8]}", created_at=_utcnow(),
    ))
    s.flush()
    return ws_id


def _make_end_user(s, *, email: str | None = None) -> str:
    euid = str(uuid.uuid4())
    s.add(EndUser(
        id=euid,
        email=email or f"eu-{euid[:8]}@example.com",
    ))
    s.flush()
    return euid


# ---------- vendor_invite_codes ---------- #


def test_invite_code_roundtrip_with_server_defaults():
    """A minimal insert (code + workspace_id + expires_at) lands with
    created_at set by server_default and consumed_at + consumed_by
    NULL."""
    code = f"inv-{uuid.uuid4()}"
    with session_scope() as s:
        ws_id = _make_workspace(s)
        s.add(VendorInviteCode(
            code=code,
            workspace_id=ws_id,
            expires_at=_utcnow() + timedelta(days=30),
        ))

    with session_scope() as s:
        row = s.get(VendorInviteCode, code)
        assert row is not None
        assert row.workspace_id == ws_id
        assert row.created_at is not None  # server_default now()
        assert row.consumed_at is None
        assert row.consumed_by_end_user_id is None


def test_invite_code_primary_key_rejects_duplicates():
    """Same code value in two rows = PK violation. (Operator
    accidentally double-inserting the same code shouldn't silently
    create two redemption surfaces.)"""
    code = f"inv-dup-{uuid.uuid4()}"
    with session_scope() as s:
        ws_id = _make_workspace(s)
        s.add(VendorInviteCode(
            code=code, workspace_id=ws_id,
            expires_at=_utcnow() + timedelta(days=30),
        ))

    from db import SessionLocal
    s2 = SessionLocal()
    try:
        with session_scope() as s:
            ws2_id = _make_workspace(s)
        s2.add(VendorInviteCode(
            code=code,  # duplicate
            workspace_id=ws2_id,
            expires_at=_utcnow() + timedelta(days=30),
        ))
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_invite_code_cascade_on_workspace_delete():
    """Delete the workspace, the code goes with it (no orphan codes
    pointing at vanished vendors)."""
    code = f"inv-cascade-{uuid.uuid4()}"
    with session_scope() as s:
        ws_id = _make_workspace(s)
        s.add(VendorInviteCode(
            code=code, workspace_id=ws_id,
            expires_at=_utcnow() + timedelta(days=30),
        ))

    with session_scope() as s:
        s.delete(s.get(Workspace, ws_id))

    with session_scope() as s:
        assert s.get(VendorInviteCode, code) is None


def test_invite_code_consumed_by_end_user_set_null_on_end_user_delete():
    """Audit-row survives end-user deletion. The end user being
    removed shouldn't blow away the vendor's bookkeeping of which
    codes were redeemed."""
    code = f"inv-audit-{uuid.uuid4()}"
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.add(VendorInviteCode(
            code=code, workspace_id=ws_id,
            expires_at=_utcnow() + timedelta(days=30),
            consumed_at=_utcnow(),
            consumed_by_end_user_id=euid,
        ))

    with session_scope() as s:
        s.delete(s.get(EndUser, euid))

    with session_scope() as s:
        row = s.get(VendorInviteCode, code)
        assert row is not None  # audit row survives
        assert row.consumed_by_end_user_id is None  # FK SET NULL fired
        assert row.consumed_at is not None  # consume timestamp preserved


def test_invite_code_index_landed():
    with session_scope() as s:
        r = s.execute(text(
            "SELECT indexname FROM pg_indexes "
            "WHERE tablename = 'vendor_invite_codes' "
            "AND indexname = 'ix_vendor_invite_codes_workspace_created'"
        )).first()
        assert r is not None


# ---------- end_user_vendor_links.display_name_override ---------- #


def test_link_display_name_override_defaults_to_null():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.execute(text(
            "INSERT INTO end_user_vendor_links (end_user_id, workspace_id) "
            "VALUES (:e, :w)"
        ), {"e": euid, "w": ws_id})

    with session_scope() as s:
        row = s.get(EndUserVendorLink, (euid, ws_id))
        assert row.display_name_override is None


def test_link_display_name_override_roundtrip():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.add(EndUserVendorLink(
            end_user_id=euid, workspace_id=ws_id,
            display_name_override="Alice Smith (JYNI)",
        ))

    with session_scope() as s:
        row = s.get(EndUserVendorLink, (euid, ws_id))
        assert row.display_name_override == "Alice Smith (JYNI)"


# ---------- end_user_vendor_links.notification_pref ---------- #


def test_link_notification_pref_server_default_all():
    """A link inserted via raw SQL without notification_pref lands
    with the server_default 'all'."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.execute(text(
            "INSERT INTO end_user_vendor_links (end_user_id, workspace_id) "
            "VALUES (:e, :w)"
        ), {"e": euid, "w": ws_id})

    with session_scope() as s:
        row = s.get(EndUserVendorLink, (euid, ws_id))
        assert row.notification_pref == "all"


@pytest.mark.parametrize("pref", ["all", "mentions", "off"])
def test_link_notification_pref_accepts_known_values(pref: str):
    with session_scope() as s:
        ws_id = _make_workspace(s)
        euid = _make_end_user(s)
        s.add(EndUserVendorLink(
            end_user_id=euid, workspace_id=ws_id,
            notification_pref=pref,
        ))

    with session_scope() as s:
        row = s.get(EndUserVendorLink, (euid, ws_id))
        assert row.notification_pref == pref


# ---------- Validator helper ---------- #


@pytest.mark.parametrize("pref", ["all", "mentions", "off"])
def test_is_valid_notification_pref_accepts(pref: str):
    assert is_valid_notification_pref(pref)


@pytest.mark.parametrize("bad", [
    "ALL", "weekly", "none", "every", "", "  all", None, 1,
])
def test_is_valid_notification_pref_rejects(bad):
    assert not is_valid_notification_pref(bad)


def test_default_notification_pref_in_frozenset():
    assert DEFAULT_NOTIFICATION_PREF in _VALID_NOTIFICATION_PREFS
    assert _VALID_NOTIFICATION_PREFS == {"all", "mentions", "off"}
