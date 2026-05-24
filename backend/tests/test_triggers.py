"""Phase 22.2: tests for the triggers helper module + CRUD endpoints.

Two surfaces:

1. Pure module (`backend/triggers.py`): validate_cron, compute_next_run_at,
   mint_webhook_token / hash_webhook_token, friendly_schedule_to_cron,
   resolve_schedule (the schedule|preset XOR validator).
2. CRUD endpoints: POST /agents/{name}/triggers (cron + webhook),
   GET /agents/{name}/triggers (list), PATCH /triggers/{id}
   (enabled / name / schedule), DELETE /triggers/{id}. Includes
   cross-workspace 404 + plaintext-token-only-on-create + schedule-
   patch-recomputes-next_run_at + delete-doesn't-delete-runs.

Scheduler loop tests live in 22.3 (worker/scheduler.py); webhook
endpoint tests in 22.6.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

import triggers as trigmod
from db import session_scope
from models import Agent, Run, Trigger
from tests.conftest import auth_headers


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _add_agent(workspace_id: str, name: str = "morning-digest") -> None:
    with session_scope() as s:
        s.add(Agent(
            workspace_id=workspace_id,
            name=name,
            role="executor",
            capabilities=[],
            command_handlers=[],
            created_at=_now(),
            updated_at=_now(),
        ))


# ---------- triggers module ---------- #


def test_validate_cron_accepts_5_field():
    trigmod.validate_cron("0 9 * * 1-5")
    trigmod.validate_cron("*/15 * * * *")


def test_validate_cron_rejects_garbage():
    for bad in ("", "   ", "not a cron", "60 * * * *", "* * *"):
        with pytest.raises(ValueError):
            trigmod.validate_cron(bad)


def test_validate_cron_rejects_non_string():
    with pytest.raises(ValueError):
        trigmod.validate_cron(None)  # type: ignore[arg-type]


def test_compute_next_run_at_advances_strictly_future():
    now = datetime(2026, 5, 24, 8, 0, 0, tzinfo=timezone.utc)
    nxt = trigmod.compute_next_run_at("0 9 * * *", now)
    assert nxt.tzinfo is not None
    assert nxt > now
    assert nxt.hour == 9 and nxt.minute == 0


def test_compute_next_run_at_requires_tz_aware():
    with pytest.raises(ValueError):
        trigmod.compute_next_run_at(
            "* * * * *",
            datetime(2026, 5, 24, 8, 0, 0),  # naive
        )


def test_friendly_schedule_to_cron_known_presets():
    assert trigmod.friendly_schedule_to_cron("daily") == "0 9 * * *"
    assert trigmod.friendly_schedule_to_cron("weekdays") == "0 9 * * 1-5"
    assert trigmod.friendly_schedule_to_cron("weekly") == "0 9 * * 1"
    assert trigmod.friendly_schedule_to_cron("hourly") == "0 * * * *"


def test_friendly_schedule_to_cron_unknown_raises():
    with pytest.raises(ValueError) as exc:
        trigmod.friendly_schedule_to_cron("yearly")
    # Error names the bad input + lists the valid set so the API
    # layer's 422 detail is operator-actionable.
    assert "yearly" in str(exc.value)
    assert "daily" in str(exc.value)


def test_mint_webhook_token_roundtrip():
    plaintext, digest = trigmod.mint_webhook_token()
    # 32 bytes URL-safe → ~43 chars; just confirm it's non-trivial.
    assert isinstance(plaintext, str) and len(plaintext) >= 40
    assert isinstance(digest, str) and len(digest) == 64
    # The hash matches when re-hashed by the public endpoint path.
    assert trigmod.hash_webhook_token(plaintext) == digest


def test_mint_webhook_token_returns_unique_values():
    """No collision risk across two consecutive mints."""
    p1, h1 = trigmod.mint_webhook_token()
    p2, h2 = trigmod.mint_webhook_token()
    assert p1 != p2
    assert h1 != h2


def test_resolve_schedule_xor_required():
    with pytest.raises(ValueError):
        trigmod.resolve_schedule(schedule=None, preset=None)
    with pytest.raises(ValueError):
        trigmod.resolve_schedule(schedule="0 9 * * *", preset="daily")


def test_resolve_schedule_prefers_preset_compilation():
    out = trigmod.resolve_schedule(schedule=None, preset="weekdays")
    assert out == "0 9 * * 1-5"


def test_resolve_schedule_validates_raw_cron():
    """A garbage raw cron raises before we ever hand it to compute."""
    with pytest.raises(ValueError):
        trigmod.resolve_schedule(schedule="not a cron", preset=None)


# ---------- POST /agents/{agent_name}/triggers ---------- #


def test_create_cron_trigger_happy_path(client, alice):
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _add_agent(ws_id)

    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(api_key),
        json={"kind": "cron", "name": "weekday 9am", "preset": "weekdays"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "cron"
    assert body["schedule"] == "0 9 * * 1-5"
    assert body["name"] == "weekday 9am"
    assert body["enabled"] is True
    assert body["next_run_at"] is not None
    # Webhook plaintext is webhook-only; cron creates never include it.
    assert "webhook_token" not in body


def test_create_webhook_trigger_returns_plaintext_once(client, alice):
    """The plaintext token comes back on the POST response and never
    again. Operator copies from the modal; the row only stores the hash."""
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _add_agent(ws_id)

    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(api_key),
        json={"kind": "webhook", "name": "zapier"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "webhook"
    assert "webhook_token" in body  # plaintext, returned once
    plaintext = body["webhook_token"]
    assert isinstance(plaintext, str) and len(plaintext) >= 40

    # Subsequent GET never includes the plaintext OR the hash.
    listed = client.get(
        "/agents/morning-digest/triggers",
        headers=auth_headers(api_key),
    ).json()["triggers"]
    assert len(listed) == 1
    assert "webhook_token" not in listed[0]
    assert "webhook_token_hash" not in listed[0]

    # The hash on disk matches the returned plaintext.
    with session_scope() as s:
        row = s.execute(select(Trigger)).scalars().one()
        assert row.webhook_token_hash == trigmod.hash_webhook_token(plaintext)


def test_create_rejects_unknown_kind(client, alice):
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _add_agent(ws_id)

    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(api_key),
        json={"kind": "event", "name": "x"},
    )
    assert r.status_code == 422


def test_create_cron_rejects_malformed_schedule(client, alice):
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _add_agent(ws_id)

    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(api_key),
        json={"kind": "cron", "name": "x", "schedule": "garbage"},
    )
    assert r.status_code == 422


def test_create_cron_rejects_schedule_and_preset_together(client, alice):
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _add_agent(ws_id)

    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(api_key),
        json={
            "kind": "cron", "name": "x",
            "schedule": "0 9 * * *", "preset": "daily",
        },
    )
    assert r.status_code == 422


def test_create_webhook_rejects_schedule_fields(client, alice):
    """Webhook triggers don't take a schedule. Schedule + preset both
    422 to make the misuse obvious."""
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _add_agent(ws_id)

    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(api_key),
        json={"kind": "webhook", "name": "x", "schedule": "0 9 * * *"},
    )
    assert r.status_code == 422


def test_create_404_on_unknown_agent(client, alice):
    api_key = alice["api_key"]["plaintext"]

    r = client.post(
        "/agents/nope/triggers",
        headers=auth_headers(api_key),
        json={"kind": "cron", "name": "x", "preset": "daily"},
    )
    assert r.status_code == 404


def test_create_requires_name(client, alice):
    ws_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _add_agent(ws_id)

    r = client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(api_key),
        json={"kind": "cron", "name": "   ", "preset": "daily"},
    )
    assert r.status_code == 422


# ---------- GET /agents/{agent_name}/triggers ---------- #


def test_list_returns_workspace_agent_triggers_only(client, alice, bob):
    """Bob's triggers don't appear in Alice's list — same-name agents
    in different workspaces stay isolated."""
    a_ws = alice["workspace"]["id"]
    b_ws = bob["workspace"]["id"]
    _add_agent(a_ws)
    _add_agent(b_ws)

    client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"kind": "cron", "name": "alice cron", "preset": "daily"},
    )
    client.post(
        "/agents/morning-digest/triggers",
        headers=auth_headers(bob["api_key"]["plaintext"]),
        json={"kind": "cron", "name": "bob cron", "preset": "daily"},
    )

    a_listed = client.get(
        "/agents/morning-digest/triggers",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    ).json()["triggers"]
    assert len(a_listed) == 1
    assert a_listed[0]["name"] == "alice cron"


def test_list_404_on_unknown_agent(client, alice):
    r = client.get(
        "/agents/nope/triggers",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404


# ---------- PATCH /triggers/{trigger_id} ---------- #


def _create_cron(client, headers, *, schedule: str = "0 9 * * *", name: str = "n"):
    r = client.post(
        "/agents/morning-digest/triggers",
        headers=headers,
        json={"kind": "cron", "name": name, "schedule": schedule},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_patch_disable_then_reenable(client, alice):
    ws_id = alice["workspace"]["id"]
    headers = auth_headers(alice["api_key"]["plaintext"])
    _add_agent(ws_id)
    t = _create_cron(client, headers)

    r = client.patch(
        f"/triggers/{t['id']}", headers=headers, json={"enabled": False},
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    r = client.patch(
        f"/triggers/{t['id']}", headers=headers, json={"enabled": True},
    )
    assert r.json()["enabled"] is True


def test_patch_schedule_recomputes_next_run_at(client, alice):
    """Changing the cron expression recomputes next_run_at so the
    scheduler's hot query picks up the new cadence on its next tick."""
    ws_id = alice["workspace"]["id"]
    headers = auth_headers(alice["api_key"]["plaintext"])
    _add_agent(ws_id)
    t = _create_cron(client, headers, schedule="0 9 * * *")
    before = t["next_run_at"]

    r = client.patch(
        f"/triggers/{t['id']}", headers=headers,
        json={"schedule": "*/5 * * * *"},  # much sooner
    )
    assert r.status_code == 200
    after = r.json()["next_run_at"]
    assert after != before  # recomputed
    assert r.json()["schedule"] == "*/5 * * * *"


def test_patch_schedule_rejected_on_webhook_trigger(client, alice):
    ws_id = alice["workspace"]["id"]
    headers = auth_headers(alice["api_key"]["plaintext"])
    _add_agent(ws_id)
    r = client.post(
        "/agents/morning-digest/triggers", headers=headers,
        json={"kind": "webhook", "name": "z"},
    )
    t_id = r.json()["id"]

    r = client.patch(
        f"/triggers/{t_id}", headers=headers,
        json={"schedule": "0 9 * * *"},
    )
    assert r.status_code == 422


def test_patch_404_on_cross_workspace(client, alice, bob):
    """Bob PATCHing Alice's trigger must 404, not silently update or
    leak existence."""
    a_ws = alice["workspace"]["id"]
    _add_agent(a_ws)
    t = _create_cron(client, auth_headers(alice["api_key"]["plaintext"]))

    r = client.patch(
        f"/triggers/{t['id']}",
        headers=auth_headers(bob["api_key"]["plaintext"]),
        json={"enabled": False},
    )
    assert r.status_code == 404


def test_patch_empty_name_rejected(client, alice):
    ws_id = alice["workspace"]["id"]
    headers = auth_headers(alice["api_key"]["plaintext"])
    _add_agent(ws_id)
    t = _create_cron(client, headers)

    r = client.patch(
        f"/triggers/{t['id']}", headers=headers, json={"name": "   "},
    )
    assert r.status_code == 422


# ---------- DELETE /triggers/{trigger_id} ---------- #


def test_delete_happy_path(client, alice):
    ws_id = alice["workspace"]["id"]
    headers = auth_headers(alice["api_key"]["plaintext"])
    _add_agent(ws_id)
    t = _create_cron(client, headers)

    r = client.delete(f"/triggers/{t['id']}", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}

    listed = client.get(
        "/agents/morning-digest/triggers", headers=headers,
    ).json()["triggers"]
    assert listed == []


def test_delete_404_on_cross_workspace(client, alice, bob):
    a_ws = alice["workspace"]["id"]
    _add_agent(a_ws)
    t = _create_cron(client, auth_headers(alice["api_key"]["plaintext"]))

    r = client.delete(
        f"/triggers/{t['id']}",
        headers=auth_headers(bob["api_key"]["plaintext"]),
    )
    assert r.status_code == 404


def test_delete_does_not_delete_past_runs(client, alice):
    """Per the spec: deleting a trigger leaves run history intact.
    Runs reference triggers via a soft link (22.4 will add the FK with
    ON DELETE SET NULL); here we just confirm a manually-linked run
    survives the trigger delete.
    """
    ws_id = alice["workspace"]["id"]
    headers = auth_headers(alice["api_key"]["plaintext"])
    _add_agent(ws_id)
    t = _create_cron(client, headers)

    # Manually create a run + point the trigger's last_run_id at it
    # (22.4's scheduled_run handler will do this for real).
    run_id = str(uuid.uuid4())
    now = _now()
    with session_scope() as s:
        s.add(Run(
            id=run_id, workspace_id=ws_id, agent_name="morning-digest",
            started_at=now, ended_at=now, cost_usd=Decimal("0"),
        ))
        row = s.get(Trigger, t["id"])
        row.last_run_id = run_id
        row.last_run_status = "succeeded"

    r = client.delete(f"/triggers/{t['id']}", headers=headers)
    assert r.status_code == 200

    # Run row is still there. (Cross-check via direct DB read; the
    # /runs endpoints aren't in scope for 22.2.)
    with session_scope() as s:
        assert s.get(Run, run_id) is not None
