"""Phase 21.9: tests for Polaris widget-incident-response.

Three surfaces:

1. Pure clustering + append-fix helpers in widget_incident_response.
2. POST /workspaces/me/widget-incident-response/scan endpoint
   (Anthropic stubbed).
3. POST .../escalations/{id}/apply-fix + .../dismiss-fix endpoints.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select

import widget_incident_response as wir
from db import session_scope
from models import (
    Agent,
    Workspace,
    WorkspaceSecret,
    WidgetConversation,
    WidgetEscalation,
    WidgetMessage,
)
from tests.conftest import auth_headers
import secrets_crypto


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _setup_workspace_with_bot(
    *,
    workspace_id: str | None = None,
    bot_name: str = "vega",
    system_prompt: str = "You are a helpful customer support bot.",
    auto_apply: bool = False,
) -> str:
    if workspace_id is None:
        workspace_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(
            id=workspace_id,
            name=f"wir-ws-{workspace_id[:8]}",
            created_at=_now(),
            customer_facing_agent_name=bot_name,
            allowed_widget_origins=["https://halo.dev"],
            polaris_auto_apply_widget_fixes=auto_apply,
        ))
        s.flush()
        s.add(Agent(
            workspace_id=workspace_id,
            name=bot_name,
            role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond", "widget:escalate"],
            command_handlers=[],
            system_prompt=system_prompt,
            description="Customer-facing FAQ bot.",
            created_at=_now(),
            updated_at=_now(),
        ))
    return workspace_id


def _seed_escalation(
    workspace_id: str,
    *,
    reason: str = "bot_escalate_call",
    user_message: str = "How do I get a refund?",
    bot_name: str = "vega",
) -> tuple[str, str]:
    """Insert a conversation + the triggering user message + an open
    escalation. Returns (conversation_id, escalation_id)."""
    conv_id = str(uuid.uuid4())
    esc_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(WidgetConversation(
            id=conv_id,
            workspace_id=workspace_id,
            customer_facing_agent_name=bot_name,
            status="escalated",
            anon_user_id=f"anon-{conv_id[:6]}",
            started_at=_now(),
            last_message_at=_now(),
        ))
        s.add(WidgetMessage(
            conversation_id=conv_id,
            role="user",
            text=user_message,
            sent_at=_now(),
        ))
        s.add(WidgetEscalation(
            id=esc_id,
            conversation_id=conv_id,
            reason=reason,
            payload={"hint": "test"},
            escalated_at=_now(),
        ))
    return conv_id, esc_id


def _seed_anthropic_key(workspace_id: str, key: str = "sk-ant-test") -> None:
    with session_scope() as s:
        s.add(WorkspaceSecret(
            workspace_id=workspace_id,
            name="ANTHROPIC_API_KEY",
            encrypted_value=secrets_crypto.encrypt(key),
            created_at=_now(),
            updated_at=_now(),
        ))


# ---------- Pure helpers (no DB) ---------- #


def test_tokenize_drops_stopwords_and_short_words():
    tokens = wir._tokenize(
        "How do I get a refund for my last order please?"
    )
    # 'a', 'i', 'do', 'my', 'for', 'how', 'please' filtered out.
    assert "refund" in tokens
    assert "order" in tokens
    assert "last" in tokens
    for stop in ("a", "do", "i", "my", "for", "how", "please"):
        assert stop not in tokens


def test_looks_similar_requires_two_token_overlap():
    a = {"refund", "billing", "order"}
    b = {"refund", "billing", "cancel"}
    assert wir._looks_similar(a, b)
    # Single-token overlap should not match.
    assert not wir._looks_similar({"refund", "order"}, {"refund", "ship"})


def test_append_fix_to_system_prompt_adds_marked_section():
    original = "You are a helpful bot."
    fix = {"detail": "When users ask about refunds, link them to /refunds."}
    applied_at = datetime(2026, 5, 23, 12, 0, 0, tzinfo=timezone.utc)
    updated = wir.append_fix_to_system_prompt(original, fix, applied_at=applied_at)
    assert original in updated
    assert "Polaris-suggested fix applied 2026-05-23" in updated
    assert "refunds, link them to /refunds" in updated


def test_append_fix_to_system_prompt_handles_null_base():
    fix = {"detail": "Be friendly."}
    updated = wir.append_fix_to_system_prompt(None, fix)
    assert "Polaris-suggested fix applied" in updated
    assert "Be friendly." in updated


# ---------- Clustering (with DB) ---------- #


def test_find_clusters_returns_empty_when_no_escalations():
    ws = _setup_workspace_with_bot()
    with session_scope() as s:
        out = wir.find_escalation_clusters(s, ws)
    assert out == []


def test_find_clusters_groups_similar_user_messages():
    """3+ escalations on the same reason with overlapping tokens
    cluster together."""
    ws = _setup_workspace_with_bot()
    for msg in [
        "How do I get a refund for my last order?",
        "Can I get a refund on my recent order?",
        "I want a refund on this order please.",
    ]:
        _seed_escalation(ws, user_message=msg)

    with session_scope() as s:
        clusters = wir.find_escalation_clusters(s, ws, min_size=3)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["size"] == 3
    assert "refund" in cluster["keywords"]
    assert len(cluster["sample_messages"]) == 3


def test_find_clusters_drops_below_min_size():
    ws = _setup_workspace_with_bot()
    _seed_escalation(ws, user_message="refund please")
    _seed_escalation(ws, user_message="refund needed")

    with session_scope() as s:
        clusters = wir.find_escalation_clusters(s, ws, min_size=3)
    assert clusters == []


def test_find_clusters_isolates_workspaces():
    ws_a = _setup_workspace_with_bot()
    ws_b = _setup_workspace_with_bot()
    for _ in range(3):
        _seed_escalation(ws_a, user_message="refund on my order please")

    with session_scope() as s:
        a_clusters = wir.find_escalation_clusters(s, ws_a, min_size=3)
        b_clusters = wir.find_escalation_clusters(s, ws_b, min_size=3)
    assert len(a_clusters) == 1
    assert b_clusters == []


def test_find_clusters_respects_lookback():
    ws = _setup_workspace_with_bot()
    # 3 escalations, all > 48 hours old.
    for i in range(3):
        conv_id, esc_id = _seed_escalation(ws, user_message="refund order please")
        with session_scope() as s:
            esc = s.get(WidgetEscalation, esc_id)
            esc.escalated_at = _now() - timedelta(hours=72)

    with session_scope() as s:
        clusters = wir.find_escalation_clusters(s, ws, lookback_hours=24, min_size=3)
    assert clusters == []


def test_find_clusters_ignores_resolved_escalations():
    ws = _setup_workspace_with_bot()
    for _ in range(3):
        _conv, esc_id = _seed_escalation(ws, user_message="refund order please")
        with session_scope() as s:
            s.get(WidgetEscalation, esc_id).resolved_at = _now()

    with session_scope() as s:
        clusters = wir.find_escalation_clusters(s, ws, min_size=3)
    assert clusters == []


# ---------- generate_suggested_fix (Anthropic stubbed) ---------- #


def _fake_anthropic_factory(canned: dict | str):
    """Return an Anthropic-shaped fake whose .messages.create
    returns the canned JSON dict (stringified) as a text block."""
    raw = (
        canned
        if isinstance(canned, str)
        else json.dumps(canned)
    )

    class _Block:
        type = "text"
        text = raw

    class _Resp:
        content = [_Block()]

    class _Messages:
        def create(self, **kwargs):
            return _Resp()

    class _Client:
        def __init__(self, *a, **kw):
            self.messages = _Messages()

    return lambda key: _Client()


def test_generate_suggested_fix_parses_canned_anthropic_response():
    cluster = {
        "reason": "bot_escalate_call",
        "escalation_ids": ["e1", "e2", "e3"],
        "sample_messages": ["I want a refund", "Refund please", "How do I refund"],
        "keywords": ["refund"],
        "size": 3,
    }
    canned = {
        "kind": "system_prompt_addendum",
        "summary": "Add refund-policy guidance",
        "detail": "When users ask about refunds, direct them to /refunds.",
    }
    fix = wir.generate_suggested_fix(
        cluster, "sk-ant-test",
        anthropic_client_factory=_fake_anthropic_factory(canned),
    )
    assert fix is not None
    assert fix["kind"] == "system_prompt_addendum"
    assert "refunds" in fix["detail"].lower()
    assert fix["keywords"] == ["refund"]
    assert fix["cluster_size"] == 3
    assert fix["generated_at"]


def test_generate_suggested_fix_strips_json_code_fences():
    cluster = {
        "reason": "bot_escalate_call",
        "escalation_ids": ["e1"],
        "sample_messages": ["test"],
        "keywords": [],
        "size": 1,
    }
    fenced = (
        '```json\n'
        '{"kind": "system_prompt_addendum", '
        '"summary": "x", "detail": "Be helpful."}\n'
        '```'
    )
    fix = wir.generate_suggested_fix(
        cluster, "sk",
        anthropic_client_factory=_fake_anthropic_factory(fenced),
    )
    assert fix is not None
    assert fix["detail"] == "Be helpful."


def test_generate_suggested_fix_rejects_invalid_shape():
    cluster = {
        "reason": "x", "escalation_ids": [], "sample_messages": [],
        "keywords": [], "size": 1,
    }
    # Bad kind value.
    bad_kind = {
        "kind": "unknown_kind", "summary": "x", "detail": "y",
    }
    out = wir.generate_suggested_fix(
        cluster, "sk",
        anthropic_client_factory=_fake_anthropic_factory(bad_kind),
    )
    assert out is None

    # Missing detail.
    no_detail = {"kind": "system_prompt_addendum", "summary": "x"}
    out = wir.generate_suggested_fix(
        cluster, "sk",
        anthropic_client_factory=_fake_anthropic_factory(no_detail),
    )
    assert out is None


def test_generate_suggested_fix_swallows_anthropic_exception():
    cluster = {
        "reason": "x", "escalation_ids": [], "sample_messages": [],
        "keywords": [], "size": 1,
    }

    class _BoomMessages:
        def create(self, **kw):
            raise RuntimeError("anthropic down")

    class _BoomClient:
        def __init__(self, *a, **kw):
            self.messages = _BoomMessages()

    out = wir.generate_suggested_fix(
        cluster, "sk", anthropic_client_factory=lambda key: _BoomClient(),
    )
    assert out is None


# ---------- POST /widget-incident-response/scan ---------- #


def test_scan_endpoint_400_without_anthropic_key(client, alice, monkeypatch):
    """No ANTHROPIC_API_KEY secret → 400 missing_anthropic_key."""
    ws_id = alice["workspace"]["id"]
    # Seed agent + 3 escalations so we get past the no-clusters early
    # return path (the missing-key check needs clusters to fire).
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond"],
            command_handlers=[],
            created_at=_now(), updated_at=_now(),
        ))
        ws = s.get(Workspace, ws_id)
        ws.customer_facing_agent_name = "vega"
    for _ in range(3):
        _seed_escalation(ws_id, user_message="refund on my order please")

    r = client.post(
        "/workspaces/me/widget-incident-response/scan",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "missing_anthropic_key"


def test_scan_endpoint_no_clusters_short_circuits(client, alice):
    """No escalations → returns zeros without needing the Anthropic
    key (skips the key check, returns immediately)."""
    r = client.post(
        "/workspaces/me/widget-incident-response/scan",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "clusters_found": 0,
        "fixes_generated": 0,
        "fixes_applied": 0,
        "conversations_touched": 0,
    }


def test_scan_endpoint_persists_fix_on_each_escalation_in_cluster(
    client, alice, monkeypatch,
):
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond"],
            command_handlers=[],
            system_prompt="You are vega.",
            created_at=_now(), updated_at=_now(),
        ))
        ws = s.get(Workspace, ws_id)
        ws.customer_facing_agent_name = "vega"
    _seed_anthropic_key(ws_id)

    esc_ids = []
    for msg in [
        "How do I get a refund for my order?",
        "Refund please on my recent order",
        "I want a refund on this order",
    ]:
        _conv, esc_id = _seed_escalation(ws_id, user_message=msg)
        esc_ids.append(esc_id)

    # Patch the Anthropic call to return a canned fix.
    canned = {
        "kind": "system_prompt_addendum",
        "summary": "Add refund FAQ",
        "detail": "Refund policy: contact support within 30 days.",
    }
    monkeypatch.setattr(
        wir, "generate_suggested_fix",
        lambda cluster, key, **kw: {
            **canned,
            "keywords": cluster.get("keywords") or [],
            "cluster_size": cluster["size"],
            "generated_at": _now().isoformat(),
        },
    )

    r = client.post(
        "/workspaces/me/widget-incident-response/scan",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clusters_found"] == 1
    assert body["fixes_generated"] == 1
    assert body["fixes_applied"] == 0  # auto-apply off
    assert body["auto_apply_enabled"] is False

    # Every escalation in the cluster now has suggested_fix populated.
    with session_scope() as s:
        for esc_id in esc_ids:
            esc = s.get(WidgetEscalation, esc_id)
            assert esc.suggested_fix is not None
            assert esc.suggested_fix["kind"] == "system_prompt_addendum"
            assert "Refund policy" in esc.suggested_fix["detail"]


def test_scan_endpoint_auto_applies_when_workspace_opts_in(
    client, alice, monkeypatch,
):
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond"],
            command_handlers=[],
            system_prompt="You are vega.",
            created_at=_now(), updated_at=_now(),
        ))
        ws = s.get(Workspace, ws_id)
        ws.customer_facing_agent_name = "vega"
        ws.polaris_auto_apply_widget_fixes = True
    _seed_anthropic_key(ws_id)

    esc_ids = []
    for _ in range(3):
        _conv, esc_id = _seed_escalation(ws_id, user_message="refund my recent order please")
        esc_ids.append(esc_id)

    canned = {
        "kind": "system_prompt_addendum",
        "summary": "Refund policy",
        "detail": "AUTO-APPLY-MARKER",
    }
    monkeypatch.setattr(
        wir, "generate_suggested_fix",
        lambda cluster, key, **kw: {
            **canned,
            "keywords": cluster.get("keywords") or [],
            "cluster_size": cluster["size"],
            "generated_at": _now().isoformat(),
        },
    )

    r = client.post(
        "/workspaces/me/widget-incident-response/scan",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    body = r.json()
    assert body["fixes_applied"] == 1
    assert body["conversations_touched"] == 3
    assert body["auto_apply_enabled"] is True

    # Bot's system_prompt mutated.
    with session_scope() as s:
        agent = s.get(Agent, (ws_id, "vega"))
        assert "AUTO-APPLY-MARKER" in (agent.system_prompt or "")
        # Escalations resolved.
        for esc_id in esc_ids:
            esc = s.get(WidgetEscalation, esc_id)
            assert esc.resolved_at is not None


# ---------- Apply / dismiss endpoints ---------- #


def test_apply_fix_mutates_system_prompt_and_resolves_escalation(client, alice):
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond"],
            command_handlers=[],
            system_prompt="Original prompt.",
            created_at=_now(), updated_at=_now(),
        ))
        ws = s.get(Workspace, ws_id)
        ws.customer_facing_agent_name = "vega"

    conv_id, esc_id = _seed_escalation(ws_id, user_message="refund?")
    with session_scope() as s:
        esc = s.get(WidgetEscalation, esc_id)
        esc.suggested_fix = {
            "kind": "system_prompt_addendum",
            "summary": "Refund guidance",
            "detail": "When users ask about refunds, link to /refunds.",
        }

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/escalations/{esc_id}/apply-fix",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    assert "vega" in body["agents_mutated"]
    assert conv_id in body["conversations_touched"]

    with session_scope() as s:
        agent = s.get(Agent, (ws_id, "vega"))
        assert "Original prompt." in agent.system_prompt
        assert "link to /refunds" in agent.system_prompt
        assert "Polaris-suggested fix applied" in agent.system_prompt

        esc = s.get(WidgetEscalation, esc_id)
        assert esc.resolved_at is not None

        conv = s.get(WidgetConversation, conv_id)
        # Status flipped back to open so bot retries with updated prompt.
        assert conv.status == "open"

        # System message dropped in the thread.
        msgs = s.execute(
            select(WidgetMessage).where(
                WidgetMessage.conversation_id == conv_id,
                WidgetMessage.role == "system",
            )
        ).scalars().all()
        assert any("Polaris updated the bot" in m.text for m in msgs)


def test_apply_fix_409_when_no_suggested_fix(client, alice):
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond"], command_handlers=[],
            created_at=_now(), updated_at=_now(),
        ))
    conv_id, esc_id = _seed_escalation(ws_id, user_message="refund?")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/escalations/{esc_id}/apply-fix",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "no_suggested_fix"


def test_apply_fix_409_when_already_resolved(client, alice):
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond"], command_handlers=[],
            created_at=_now(), updated_at=_now(),
        ))
    conv_id, esc_id = _seed_escalation(ws_id, user_message="x")
    with session_scope() as s:
        esc = s.get(WidgetEscalation, esc_id)
        esc.suggested_fix = {
            "kind": "system_prompt_addendum",
            "summary": "x", "detail": "y",
        }
        esc.resolved_at = _now()

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/escalations/{esc_id}/apply-fix",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 409


def test_apply_fix_404_cross_workspace(client, alice):
    other_ws = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Workspace(id=other_ws, name="other-co", created_at=_now()))
    conv_id, esc_id = _seed_escalation(other_ws, user_message="x")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/escalations/{esc_id}/apply-fix",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 404


def test_dismiss_fix_clears_field(client, alice):
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond"], command_handlers=[],
            created_at=_now(), updated_at=_now(),
        ))
    conv_id, esc_id = _seed_escalation(ws_id, user_message="x")
    with session_scope() as s:
        esc = s.get(WidgetEscalation, esc_id)
        esc.suggested_fix = {
            "kind": "system_prompt_addendum", "summary": "x", "detail": "y",
        }

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/escalations/{esc_id}/dismiss-fix",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    assert r.json()["dismissed"] is True

    with session_scope() as s:
        esc = s.get(WidgetEscalation, esc_id)
        assert esc.suggested_fix is None
        # Escalation stays open after dismiss — operator can still
        # take over / resolve.
        assert esc.resolved_at is None


def test_dismiss_fix_idempotent_when_field_already_null(client, alice):
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        s.add(Agent(
            workspace_id=ws_id, name="vega", role="specialist",
            sensitivity_level="public",
            capabilities=["widget:respond"], command_handlers=[],
            created_at=_now(), updated_at=_now(),
        ))
    conv_id, esc_id = _seed_escalation(ws_id, user_message="x")

    r = client.post(
        f"/workspaces/me/inbox/{conv_id}/escalations/{esc_id}/dismiss-fix",
        headers=auth_headers(alice["api_key"]["plaintext"]),
    )
    assert r.status_code == 200
    assert r.json()["noop"] is True


# ---------- PATCH /workspaces/me with auto-apply setting ---------- #


def test_workspace_patch_sets_auto_apply_flag(client, alice):
    r = client.patch(
        "/workspaces/me",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"polaris_auto_apply_widget_fixes": True},
    )
    assert r.status_code == 200
    assert r.json()["polaris_auto_apply_widget_fixes"] is True

    with session_scope() as s:
        ws = s.get(Workspace, alice["workspace"]["id"])
        assert ws.polaris_auto_apply_widget_fixes is True


def test_workspace_patch_clears_auto_apply_flag(client, alice):
    ws_id = alice["workspace"]["id"]
    with session_scope() as s:
        s.get(Workspace, ws_id).polaris_auto_apply_widget_fixes = True

    r = client.patch(
        "/workspaces/me",
        headers=auth_headers(alice["api_key"]["plaintext"]),
        json={"polaris_auto_apply_widget_fixes": False},
    )
    assert r.status_code == 200
    assert r.json()["polaris_auto_apply_widget_fixes"] is False
