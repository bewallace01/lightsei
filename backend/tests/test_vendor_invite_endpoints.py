"""Phase 27.2: tests for vendor invite codes + per-vendor end-user
settings endpoints.

Three operator-side + four end-user-side endpoints under test:

  POST   /workspaces/me/end-user-invites
  GET    /workspaces/me/end-user-invites
  DELETE /workspaces/me/end-user-invites/{code}
  POST   /me/end-user/redeem-invite
  GET    /me/end-user/vendors
  PATCH  /me/end-user/vendors/{workspace_id}
  DELETE /me/end-user/vendors/{workspace_id}
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from db import session_scope
from keys import generate_session_token, hash_token
from models import (
    EndUser,
    EndUserSession,
    EndUserVendorLink,
    VendorInviteCode,
    Workspace,
)
from tests.conftest import auth_headers, signup


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_end_user(*, email: str | None = None) -> tuple[str, str]:
    euid = str(uuid.uuid4())
    token = generate_session_token()
    with session_scope() as s:
        s.add(EndUser(
            id=euid, email=email or f"eu-{euid[:8]}@example.com",
        ))
        s.flush()
        s.add(EndUserSession(
            id=str(uuid.uuid4()),
            end_user_id=euid,
            token_hash=hash_token(token),
            created_at=_now(),
            expires_at=_now() + timedelta(days=30),
        ))
    return euid, token


def _eu_auth(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


# ---------- POST /workspaces/me/end-user-invites ---------- #


def test_mint_invites_requires_auth(client):
    r = client.post("/workspaces/me/end-user-invites", json={"count": 1})
    assert r.status_code == 401


def test_mint_invites_default_one_code(client, alice):
    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 1},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["codes"]) == 1
    code = body["codes"][0]
    assert code["code"].startswith("inv-")
    assert code["workspace_id"] == alice["workspace"]["id"]
    assert code["consumed_at"] is None


def test_mint_invites_bulk_count(client, alice):
    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 5},
    )
    assert r.status_code == 200
    codes = r.json()["codes"]
    assert len(codes) == 5
    # All unique.
    assert len({c["code"] for c in codes}) == 5


def test_mint_invites_rejects_zero_and_over_cap(client, alice):
    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 0},
    )
    assert r.status_code == 422

    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 101},
    )
    assert r.status_code == 422


def test_mint_invites_ttl_days_respected(client, alice):
    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 1, "ttl_days": 7},
    )
    code = r.json()["codes"][0]
    delta = datetime.fromisoformat(code["expires_at"]) - datetime.fromisoformat(
        code["created_at"]
    )
    assert 6.5 < delta.total_seconds() / 86400 < 7.5


# ---------- GET /workspaces/me/end-user-invites ---------- #


def test_list_invites_default_excludes_consumed_and_expired(client, alice):
    # Mint 3 codes via the endpoint.
    client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 3},
    )
    # Mark one consumed + one expired directly in the DB.
    with session_scope() as s:
        rows = s.execute(
            select(VendorInviteCode).where(
                VendorInviteCode.workspace_id == alice["workspace"]["id"]
            )
        ).scalars().all()
        rows[0].consumed_at = _now()
        rows[1].expires_at = _now() - timedelta(seconds=1)

    r = client.get(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 200
    codes = r.json()["codes"]
    # Only the third (not consumed, not expired) remains.
    assert len(codes) == 1


def test_list_invites_include_consumed_and_expired(client, alice):
    client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 3},
    )
    with session_scope() as s:
        rows = s.execute(
            select(VendorInviteCode).where(
                VendorInviteCode.workspace_id == alice["workspace"]["id"]
            )
        ).scalars().all()
        rows[0].consumed_at = _now()
        rows[1].expires_at = _now() - timedelta(seconds=1)

    r = client.get(
        "/workspaces/me/end-user-invites?include_consumed=true&include_expired=true",
        headers=auth_headers(alice["session_token"]),
    )
    assert len(r.json()["codes"]) == 3


def test_list_invites_workspace_scoped(client, alice, bob):
    """Operator B's codes don't appear in operator A's list."""
    client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(bob["session_token"]),
        json={"count": 2},
    )
    client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 1},
    )
    r = client.get(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
    )
    codes = r.json()["codes"]
    assert all(c["workspace_id"] == alice["workspace"]["id"] for c in codes)
    assert len(codes) == 1


# ---------- DELETE /workspaces/me/end-user-invites/{code} ---------- #


def test_revoke_unconsumed_code_succeeds(client, alice):
    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 1},
    )
    code = r.json()["codes"][0]["code"]
    r2 = client.delete(
        f"/workspaces/me/end-user-invites/{code}",
        headers=auth_headers(alice["session_token"]),
    )
    assert r2.status_code == 200
    assert r2.json() == {"revoked": True, "code": code}
    # Confirmed gone.
    with session_scope() as s:
        assert s.get(VendorInviteCode, code) is None


def test_revoke_consumed_code_returns_404(client, alice):
    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(alice["session_token"]),
        json={"count": 1},
    )
    code = r.json()["codes"][0]["code"]
    with session_scope() as s:
        s.get(VendorInviteCode, code).consumed_at = _now()
    r2 = client.delete(
        f"/workspaces/me/end-user-invites/{code}",
        headers=auth_headers(alice["session_token"]),
    )
    assert r2.status_code == 404


def test_revoke_other_workspaces_code_returns_404(client, alice, bob):
    """Operator A can't revoke operator B's code — same 404 shape so
    op A can't probe whether a specific code exists in op B's list."""
    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(bob["session_token"]),
        json={"count": 1},
    )
    bob_code = r.json()["codes"][0]["code"]
    r2 = client.delete(
        f"/workspaces/me/end-user-invites/{bob_code}",
        headers=auth_headers(alice["session_token"]),
    )
    assert r2.status_code == 404


def test_revoke_unknown_code_returns_404(client, alice):
    r = client.delete(
        "/workspaces/me/end-user-invites/inv-does-not-exist",
        headers=auth_headers(alice["session_token"]),
    )
    assert r.status_code == 404


# ---------- POST /me/end-user/redeem-invite ---------- #


def _mint_code(client, operator) -> str:
    r = client.post(
        "/workspaces/me/end-user-invites",
        headers=auth_headers(operator["session_token"]),
        json={"count": 1},
    )
    return r.json()["codes"][0]["code"]


def test_redeem_requires_end_user_auth(client):
    r = client.post(
        "/me/end-user/redeem-invite", json={"code": "anything"},
    )
    assert r.status_code == 401


def test_redeem_happy_path_creates_link_and_consumes_code(client, alice):
    code = _mint_code(client, alice)
    eu_id, eu_token = _make_end_user()

    r = client.post(
        "/me/end-user/redeem-invite",
        headers=_eu_auth(eu_token),
        json={"code": code},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["linked"] is True
    assert body["vendor"]["id"] == alice["workspace"]["id"]
    assert body["link"]["linked_via"] == "invite_code"
    assert body["link"]["notification_pref"] == "all"

    with session_scope() as s:
        link = s.get(
            EndUserVendorLink, (eu_id, alice["workspace"]["id"]),
        )
        assert link is not None
        assert link.removed_at is None

        code_row = s.get(VendorInviteCode, code)
        assert code_row.consumed_at is not None
        assert code_row.consumed_by_end_user_id == eu_id


def test_redeem_unknown_code_returns_422(client):
    _, eu_token = _make_end_user()
    r = client.post(
        "/me/end-user/redeem-invite",
        headers=_eu_auth(eu_token),
        json={"code": "inv-totally-fake"},
    )
    assert r.status_code == 422


def test_redeem_expired_code_returns_422(client, alice):
    code = _mint_code(client, alice)
    with session_scope() as s:
        s.get(VendorInviteCode, code).expires_at = _now() - timedelta(seconds=1)

    _, eu_token = _make_end_user()
    r = client.post(
        "/me/end-user/redeem-invite",
        headers=_eu_auth(eu_token),
        json={"code": code},
    )
    assert r.status_code == 422


def test_redeem_already_consumed_code_returns_422(client, alice):
    code = _mint_code(client, alice)
    _, eu_token = _make_end_user()
    # First redeem succeeds.
    r1 = client.post(
        "/me/end-user/redeem-invite",
        headers=_eu_auth(eu_token),
        json={"code": code},
    )
    assert r1.status_code == 200
    # Second redeem of the same code by anyone (same or different
    # end user) fails.
    _, eu_token_2 = _make_end_user()
    r2 = client.post(
        "/me/end-user/redeem-invite",
        headers=_eu_auth(eu_token_2),
        json={"code": code},
    )
    assert r2.status_code == 422


def test_redeem_reactivates_soft_removed_link(client, alice):
    """End user previously unsubscribed (removed_at set), then redeems
    a new code → removed_at gets cleared, link re-active."""
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id,
            workspace_id=alice["workspace"]["id"],
            removed_at=_now() - timedelta(days=1),
        ))

    code = _mint_code(client, alice)
    r = client.post(
        "/me/end-user/redeem-invite",
        headers=_eu_auth(eu_token),
        json={"code": code},
    )
    assert r.status_code == 200

    with session_scope() as s:
        link = s.get(
            EndUserVendorLink, (eu_id, alice["workspace"]["id"]),
        )
        assert link.removed_at is None


# ---------- GET /me/end-user/vendors ---------- #


def test_list_end_user_vendors_returns_unread_count_field(client, alice):
    """v1: unread_count always 0 (parked follow-up). The field IS on
    the response shape so the dashboard can render the badge slot."""
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id, workspace_id=alice["workspace"]["id"],
        ))

    r = client.get("/me/end-user/vendors", headers=_eu_auth(eu_token))
    assert r.status_code == 200
    vendors = r.json()["vendors"]
    assert len(vendors) == 1
    v = vendors[0]
    assert v["id"] == alice["workspace"]["id"]
    assert v["unread_count"] == 0


def test_list_end_user_vendors_requires_auth(client):
    r = client.get("/me/end-user/vendors")
    assert r.status_code == 401


def test_list_end_user_vendors_excludes_soft_removed(client, alice):
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id, workspace_id=alice["workspace"]["id"],
            removed_at=_now(),
        ))

    r = client.get("/me/end-user/vendors", headers=_eu_auth(eu_token))
    assert r.json()["vendors"] == []


# ---------- PATCH /me/end-user/vendors/{workspace_id} ---------- #


def test_patch_vendor_settings_updates_notification_pref(client, alice):
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id, workspace_id=alice["workspace"]["id"],
        ))

    r = client.patch(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
        json={"notification_pref": "off"},
    )
    assert r.status_code == 200
    assert r.json()["notification_pref"] == "off"


def test_patch_vendor_settings_invalid_notification_pref_422(client, alice):
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id, workspace_id=alice["workspace"]["id"],
        ))

    r = client.patch(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
        json={"notification_pref": "weekly"},
    )
    assert r.status_code == 422


def test_patch_vendor_settings_display_name_override(client, alice):
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id, workspace_id=alice["workspace"]["id"],
        ))

    r = client.patch(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
        json={"display_name_override": "Alice S."},
    )
    assert r.status_code == 200
    assert r.json()["display_name_override"] == "Alice S."

    # Empty string clears.
    r2 = client.patch(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
        json={"display_name_override": ""},
    )
    assert r2.json()["display_name_override"] is None


def test_patch_vendor_settings_404_when_not_linked(client, alice):
    _, eu_token = _make_end_user()
    r = client.patch(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
        json={"notification_pref": "off"},
    )
    assert r.status_code == 404


def test_patch_vendor_settings_404_when_soft_removed(client, alice):
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id, workspace_id=alice["workspace"]["id"],
            removed_at=_now(),
        ))
    r = client.patch(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
        json={"notification_pref": "off"},
    )
    assert r.status_code == 404


# ---------- DELETE /me/end-user/vendors/{workspace_id} ---------- #


def test_soft_revoke_sets_removed_at(client, alice):
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id, workspace_id=alice["workspace"]["id"],
        ))

    r = client.delete(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
    )
    assert r.status_code == 200
    assert r.json() == {"unlinked": True, "workspace_id": alice["workspace"]["id"]}

    with session_scope() as s:
        link = s.get(
            EndUserVendorLink, (eu_id, alice["workspace"]["id"]),
        )
        assert link is not None  # not hard-deleted
        assert link.removed_at is not None


def test_soft_revoke_idempotent_404(client, alice):
    eu_id, eu_token = _make_end_user()
    with session_scope() as s:
        s.add(EndUserVendorLink(
            end_user_id=eu_id, workspace_id=alice["workspace"]["id"],
        ))

    client.delete(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
    )
    # Second DELETE on already-removed link.
    r = client.delete(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
    )
    assert r.status_code == 404


def test_soft_revoke_404_when_unlinked(client, alice):
    _, eu_token = _make_end_user()
    r = client.delete(
        f"/me/end-user/vendors/{alice['workspace']['id']}",
        headers=_eu_auth(eu_token),
    )
    assert r.status_code == 404
