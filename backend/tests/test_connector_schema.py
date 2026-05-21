"""Phase 20.1: tests for the connector schema + registry backbone.

Two surfaces:

1. `connector_installations` table — roundtrip + FK cascade + the
   partial-unique index that enforces one active install per
   (workspace_id, connector_type).
2. `CONNECTOR_REGISTRY` invariants — three v1 entries (gmail /
   google_calendar / google_drive), each with valid declared_zones,
   non-empty default_scopes, deterministic ordering.

Connector OAuth + per-tool implementation tests land in their own
files (20.2-20.5).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from connectors import (
    CONNECTOR_REGISTRY,
    ConnectorNotImplementedError,
    get_connector,
    list_connectors,
)
from db import session_scope
from models import (
    ConnectorInstallation,
    Workspace,
    _VALID_SENSITIVITY_LEVELS,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _make_workspace(s) -> str:
    """Insert a Lightsei workspace + return its id."""
    ws_id = str(uuid.uuid4())
    s.add(
        Workspace(
            id=ws_id,
            name=f"connector-schema-test-{ws_id[:8]}",
            created_at=_utcnow(),
        )
    )
    s.flush()
    return ws_id


def _install(
    s,
    workspace_id: str,
    *,
    connector_type: str = "gmail",
    revoked: bool = False,
    external_account_email: str = "ops@example.com",
) -> str:
    """Helper to insert a connector install. Returns the row id."""
    install_id = str(uuid.uuid4())
    s.add(
        ConnectorInstallation(
            id=install_id,
            workspace_id=workspace_id,
            connector_type=connector_type,
            encrypted_tokens=b"fake-encrypted-blob",
            scopes=["openid", "email"],
            installed_by_user_id=None,
            external_account_email=external_account_email,
            installed_at=_utcnow(),
            revoked_at=_utcnow() if revoked else None,
        )
    )
    s.flush()
    return install_id


# ---------- connector_installations roundtrip ---------- #


def test_install_roundtrip():
    with session_scope() as s:
        ws_id = _make_workspace(s)
        install_id = _install(s, ws_id)

    with session_scope() as s:
        row = s.get(ConnectorInstallation, install_id)
        assert row is not None
        assert row.connector_type == "gmail"
        assert row.scopes == ["openid", "email"]
        assert row.encrypted_tokens == b"fake-encrypted-blob"
        assert row.revoked_at is None


def test_install_fk_cascades_on_workspace_delete():
    """Tearing down a workspace tears down its connector installs."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        install_id = _install(s, ws_id)

    with session_scope() as s:
        s.delete(s.get(Workspace, ws_id))

    with session_scope() as s:
        assert s.get(ConnectorInstallation, install_id) is None


# ---------- partial-unique enforcement ---------- #


def test_partial_unique_one_active_install_per_type():
    """Two active installs for the same (workspace, connector_type)
    violate the partial-unique index."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _install(s, ws_id, connector_type="gmail")

    # Inline insert without flush so the IntegrityError fires on
    # commit — same pattern as test_slack_schema's partial-unique
    # tests. The _install helper calls flush() which would surface
    # the error too early to catch with pytest.raises around commit.
    from db import SessionLocal
    s2 = SessionLocal()
    try:
        s2.add(
            ConnectorInstallation(
                id=str(uuid.uuid4()),
                workspace_id=ws_id,
                connector_type="gmail",  # collision
                encrypted_tokens=b"fake",
                scopes=["openid"],
                external_account_email="other@example.com",
                installed_at=_utcnow(),
            )
        )
        with pytest.raises(IntegrityError):
            s2.commit()
    finally:
        s2.rollback()
        s2.close()


def test_partial_unique_allows_different_connector_types():
    """Same workspace, different connector_types coexist."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _install(s, ws_id, connector_type="gmail")
        _install(s, ws_id, connector_type="google_calendar")
        _install(s, ws_id, connector_type="google_drive")

    with session_scope() as s:
        rows = s.execute(
            select(ConnectorInstallation).where(
                ConnectorInstallation.workspace_id == ws_id
            )
        ).scalars().all()
        assert len(rows) == 3


def test_partial_unique_allows_reinstall_after_revoke():
    """Once revoked, a fresh install of the same connector works."""
    with session_scope() as s:
        ws_id = _make_workspace(s)
        _install(s, ws_id, connector_type="gmail", revoked=True)

    # Fresh install should succeed.
    with session_scope() as s:
        _install(s, ws_id, connector_type="gmail")

    with session_scope() as s:
        rows = s.execute(
            select(ConnectorInstallation).where(
                ConnectorInstallation.workspace_id == ws_id,
                ConnectorInstallation.connector_type == "gmail",
            )
        ).scalars().all()
        assert len(rows) == 2  # both visible; index only restricted writes


def test_partial_unique_isolates_tenants():
    """Workspace A's gmail install + Workspace B's gmail install
    coexist — the partial-unique is scoped by workspace_id."""
    with session_scope() as s:
        ws_a = _make_workspace(s)
        ws_b = _make_workspace(s)
        _install(s, ws_a, connector_type="gmail")
        _install(s, ws_b, connector_type="gmail")

    with session_scope() as s:
        rows = s.execute(
            select(ConnectorInstallation).where(
                ConnectorInstallation.connector_type == "gmail"
            )
        ).scalars().all()
        assert len(rows) == 2


# ---------- index existence checks ---------- #


def test_partial_unique_index_landed_in_pg_indexes():
    with session_scope() as s:
        r = s.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'connector_installations' "
                "AND indexname = 'ix_connector_installations_ws_type_active'"
            )
        ).first()
        assert r is not None


def test_workspace_browse_index_landed():
    with session_scope() as s:
        r = s.execute(
            text(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'connector_installations' "
                "AND indexname = 'ix_connector_installations_workspace_installed_at'"
            )
        ).first()
        assert r is not None


# ---------- CONNECTOR_REGISTRY shape ---------- #


def test_registry_has_v1_connectors():
    """Three v1 connectors: gmail, google_calendar, google_drive."""
    assert set(CONNECTOR_REGISTRY.keys()) == {
        "gmail", "google_calendar", "google_drive",
    }


def test_every_registry_entry_declares_valid_zones():
    """declared_zones must be a subset of the real sensitivity ladder.
    ConnectorSpec.__post_init__ enforces this at construction time,
    but verify it on the actual registry as defense-in-depth."""
    for name, spec in CONNECTOR_REGISTRY.items():
        bad = set(spec.declared_zones) - _VALID_SENSITIVITY_LEVELS
        assert not bad, f"{name} declared_zones has invalid: {bad}"


def test_every_registry_entry_has_default_scopes():
    """OAuth without scopes is meaningless."""
    for name, spec in CONNECTOR_REGISTRY.items():
        assert spec.default_scopes, f"{name} has no default_scopes"


def test_every_registry_entry_has_oauth_provider():
    """The OAuth helper module is keyed by oauth_provider; null would
    short-circuit the install flow."""
    for name, spec in CONNECTOR_REGISTRY.items():
        assert spec.oauth_provider, f"{name} has no oauth_provider"


def test_google_connectors_share_oauth_provider():
    """All three v1 connectors share Google's OAuth flow — the whole
    point of the v1 set choice. If this breaks, we're paying for
    three OAuth integrations instead of one."""
    for name in ("gmail", "google_calendar", "google_drive"):
        assert CONNECTOR_REGISTRY[name].oauth_provider == "google"


def test_gmail_zones_exclude_public():
    """Wedge invariant: a public-zoned research bot has no business
    in the email inbox. The registry must declare Gmail not-safe in
    the public zone."""
    assert "public" not in CONNECTOR_REGISTRY["gmail"].declared_zones


def test_calendar_zones_include_public():
    """Scheduling spans every zone (public research → external
    meetings; internal → team standups; pii → customer check-ins)."""
    assert "public" in CONNECTOR_REGISTRY["google_calendar"].declared_zones


def test_drive_zones_exclude_public():
    """Workspace docs are internal at minimum."""
    assert "public" not in CONNECTOR_REGISTRY["google_drive"].declared_zones


# ---------- registry lookup helpers ---------- #


def test_get_connector_returns_spec():
    spec = get_connector("gmail")
    assert spec is not None
    assert spec.name == "gmail"
    assert spec.display_label == "Gmail"


def test_get_connector_returns_none_for_unknown():
    assert get_connector("definitely_not_a_real_connector") is None


def test_list_connectors_is_stable_order():
    """Card grid in the dashboard mustn't shuffle on every render.
    Stable order = registry declaration order."""
    a = [s.name for s in list_connectors()]
    b = [s.name for s in list_connectors()]
    assert a == b
    assert a == ["gmail", "google_calendar", "google_drive"]


# ---------- ConnectorSpec validation ---------- #


def test_connector_spec_rejects_invalid_zone():
    """A typo in declared_zones should fail loud at construction
    time, not silently lock the connector out of every bot."""
    from connectors import ConnectorSpec
    with pytest.raises(ValueError) as exc:
        ConnectorSpec(
            name="oops",
            display_label="Oops",
            oauth_provider="google",
            default_scopes=("openid",),
            declared_zones=frozenset({"internal", "TOP_SECRET"}),
            summary="bad",
            manifest=lambda: [],
            invoke=lambda **kw: None,
        )
    assert "TOP_SECRET" in str(exc.value)


# ---------- Stub invoke behavior ---------- #


def test_invoke_raises_not_implemented_before_phase_20_3():
    """The 20.1 stubs raise ConnectorNotImplementedError so the bot-
    callable endpoint in 20.6 can surface 'this connector isn't
    ready yet' rather than crash."""
    spec = CONNECTOR_REGISTRY["gmail"]
    with pytest.raises(ConnectorNotImplementedError) as exc:
        spec.invoke(tool_name="send_email", payload={}, access_token="t")
    assert "gmail" in str(exc.value)
    assert "send_email" in str(exc.value)
