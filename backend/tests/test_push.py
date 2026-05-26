"""Phase 28.2: tests for backend/push.py.

Three surfaces:

1. Tri-state env machinery (FAKE_CAPTURE / live / REQUIRE_LIVE), same
   shape as the operator email_provider tests + the
   `feedback_external_service_require_live` memory.
2. `send_to_end_user` happy path in capture mode: queries active
   subs, captures payload per sub, bumps `last_used_at`, returns
   summary.
3. Filtering: revoked subs are skipped; unknown end_user is no-op.

The 410-Gone cleanup behavior + live HTTP delivery happen inside
the `pywebpush` branch which we don't exercise here (pywebpush is
lazy-imported + only used in live mode). Live-mode coverage is
verified manually in prod after the Phase 28.6 sweep + a real
push subscription on Bailey's iPhone.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

import push
from db import session_scope
from models import EndUser, EndUserPushSubscription


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def _reset_capture():
    push._reset_for_tests()
    yield
    push._reset_for_tests()


def _make_end_user(s, *, email: str | None = None) -> str:
    euid = str(uuid.uuid4())
    s.add(EndUser(
        id=euid, email=email or f"eu-{euid[:8]}@example.com",
    ))
    s.flush()
    return euid


def _make_sub(
    s, end_user_id: str, *,
    endpoint: str | None = None,
    revoked: bool = False,
) -> str:
    sid = str(uuid.uuid4())
    s.add(EndUserPushSubscription(
        id=sid,
        end_user_id=end_user_id,
        endpoint=endpoint or f"https://push.example.com/{sid}",
        p256dh="BAaaaa-fake-p256dh",
        auth="BBbbbb-fake-auth",
        revoked_at=_utcnow() if revoked else None,
    ))
    s.flush()
    return sid


# ---------- Tri-state env machinery ---------- #


def test_capture_mode_when_no_keys_set(monkeypatch):
    """No keys → capture mode (the dev/test default). _is_live
    returns False even if FAKE_CAPTURE is also off."""
    monkeypatch.delenv("LIGHTSEI_VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LIGHTSEI_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("LIGHTSEI_PUSH_FAKE_CAPTURE", raising=False)
    monkeypatch.delenv("LIGHTSEI_PUSH_REQUIRE_LIVE", raising=False)
    assert push._is_live() is False


def test_fake_capture_wins_when_keys_present(monkeypatch):
    """Keys present but FAKE_CAPTURE forced — tests stay
    network-free even when the env looks prod-shaped."""
    monkeypatch.setenv("LIGHTSEI_VAPID_PUBLIC_KEY", "fake-pub")
    monkeypatch.setenv("LIGHTSEI_VAPID_PRIVATE_KEY", "fake-priv")
    monkeypatch.setenv("LIGHTSEI_PUSH_FAKE_CAPTURE", "1")
    assert push._is_live() is False


def test_require_live_without_keys_raises(monkeypatch):
    """REQUIRE_LIVE + no keys = hard error on send. Prevents the
    silent-capture-on-prod misconfig from
    `feedback_external_service_require_live`."""
    monkeypatch.delenv("LIGHTSEI_VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LIGHTSEI_VAPID_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("LIGHTSEI_PUSH_FAKE_CAPTURE", raising=False)
    monkeypatch.setenv("LIGHTSEI_PUSH_REQUIRE_LIVE", "true")

    with session_scope() as s:
        euid = _make_end_user(s)
        _make_sub(s, euid)

    with session_scope() as s:
        with pytest.raises(push.PushNotConfiguredError):
            push.send_to_end_user(
                s, euid, title="t", body="b",
            )


def test_fake_capture_wins_over_require_live(monkeypatch):
    """Both REQUIRE_LIVE + FAKE_CAPTURE set → FAKE_CAPTURE wins.
    Lets tests force capture even against prod-shaped env."""
    monkeypatch.setenv("LIGHTSEI_PUSH_REQUIRE_LIVE", "true")
    monkeypatch.setenv("LIGHTSEI_PUSH_FAKE_CAPTURE", "1")
    monkeypatch.delenv("LIGHTSEI_VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LIGHTSEI_VAPID_PRIVATE_KEY", raising=False)

    with session_scope() as s:
        euid = _make_end_user(s)
        _make_sub(s, euid)

    with session_scope() as s:
        summary = push.send_to_end_user(
            s, euid, title="t", body="b",
        )
        # FAKE_CAPTURE doesn't raise; sends via capture path.
        assert summary["sent"] == 1
    assert len(push.captured_pushes()) == 1


# ---------- send_to_end_user happy path (capture mode) ---------- #


def test_send_captures_payload_per_subscription():
    """One end user with two devices → two captured entries, summary
    counts 2 sent / 0 failed / 0 revoked."""
    with session_scope() as s:
        euid = _make_end_user(s)
        _make_sub(s, euid, endpoint="https://push.example.com/device-a")
        _make_sub(s, euid, endpoint="https://push.example.com/device-b")

    with session_scope() as s:
        summary = push.send_to_end_user(
            s, euid,
            title="New message from JYNI",
            body="vega replied to your refund question",
            deep_link_url="/c/jyni/conversation/abc",
        )

    assert summary == {
        "sent": 2,
        "failed": 0,
        "revoked": 0,
        "total_subs": 2,
    }
    captured = push.captured_pushes()
    assert len(captured) == 2
    endpoints = {c["endpoint"] for c in captured}
    assert endpoints == {
        "https://push.example.com/device-a",
        "https://push.example.com/device-b",
    }
    for c in captured:
        assert c["end_user_id"] == euid
        assert c["payload"]["title"] == "New message from JYNI"
        assert c["payload"]["body"] == "vega replied to your refund question"
        assert c["payload"]["deep_link_url"] == "/c/jyni/conversation/abc"


def test_send_bumps_last_used_at_in_capture_mode():
    """Capture mode mirrors the live-path side effects so tests
    asserting on last_used_at work whether or not VAPID is
    configured."""
    with session_scope() as s:
        euid = _make_end_user(s)
        sid = _make_sub(s, euid)

    with session_scope() as s:
        push.send_to_end_user(s, euid, title="t", body="b")

    with session_scope() as s:
        row = s.get(EndUserPushSubscription, sid)
        assert row.last_used_at is not None


def test_send_skips_revoked_subscriptions():
    """A subscription with revoked_at set doesn't get captured + the
    summary's total_subs reflects only active rows."""
    with session_scope() as s:
        euid = _make_end_user(s)
        _make_sub(s, euid, endpoint="https://push.example.com/active")
        _make_sub(s, euid, endpoint="https://push.example.com/dead", revoked=True)

    with session_scope() as s:
        summary = push.send_to_end_user(s, euid, title="t", body="b")

    assert summary["total_subs"] == 1
    assert summary["sent"] == 1
    assert len(push.captured_pushes()) == 1
    assert push.captured_pushes()[0]["endpoint"].endswith("/active")


def test_send_to_end_user_with_no_subs_is_noop():
    """No subscriptions = no-op. Returns the empty-counts summary
    so callers can treat the case uniformly."""
    with session_scope() as s:
        euid = _make_end_user(s)

    with session_scope() as s:
        summary = push.send_to_end_user(s, euid, title="t", body="b")

    assert summary == {
        "sent": 0, "failed": 0, "revoked": 0, "total_subs": 0,
    }
    assert push.captured_pushes() == []


def test_send_to_nonexistent_end_user_is_noop():
    """Unknown end_user_id falls through cleanly (no subs match)
    rather than raising."""
    fake_id = str(uuid.uuid4())
    with session_scope() as s:
        summary = push.send_to_end_user(s, fake_id, title="t", body="b")
    assert summary["total_subs"] == 0
    assert summary["sent"] == 0


# ---------- Cross-end-user isolation ---------- #


def test_send_only_targets_specified_end_users_subs():
    """Alice + Bob each have a subscription; sending to Alice
    captures only Alice's row."""
    with session_scope() as s:
        alice = _make_end_user(s, email="alice@example.com")
        bob = _make_end_user(s, email="bob@example.com")
        _make_sub(s, alice, endpoint="https://push.example.com/alice")
        _make_sub(s, bob, endpoint="https://push.example.com/bob")

    with session_scope() as s:
        summary = push.send_to_end_user(s, alice, title="t", body="b")

    assert summary["sent"] == 1
    captured = push.captured_pushes()
    assert len(captured) == 1
    assert captured[0]["end_user_id"] == alice
    assert captured[0]["endpoint"].endswith("/alice")


# ---------- Payload shape ---------- #


def test_payload_omits_optional_fields_when_unset():
    """deep_link_url + icon_url default to None → not in payload.
    Service worker (Phase 28.4) treats their absence as 'no
    deep-link / use the default icon'."""
    with session_scope() as s:
        euid = _make_end_user(s)
        _make_sub(s, euid)

    with session_scope() as s:
        push.send_to_end_user(s, euid, title="t", body="b")

    p = push.captured_pushes()[0]["payload"]
    assert p == {"title": "t", "body": "b"}
    assert "deep_link_url" not in p
    assert "icon" not in p
