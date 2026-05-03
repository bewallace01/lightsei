"""Phase 11.2: backend dispatch chain machinery.

Covers the schema additions on `commands` (chain id, depth, source,
approval state) plus the new caps on `agents` (max_dispatch_depth,
max_dispatch_per_day) and the auto-approval rules table. Drives all
the new HTTP surfaces: enqueue with chain attribution, claim's
approval-gated filter, the approve/reject endpoints, the auto-approval
rule CRUD, and the constellation edges populated from this data.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from db import session_scope
from models import Command
from tests.conftest import auth_headers


# ---------- enqueue: chain id + depth + source attribution ---------- #


def test_enqueue_user_origin_auto_approves(client, alice):
    """A user-initiated enqueue (no source_agent) gets a fresh chain
    id, depth=0, and lands as auto_approved — the user is already
    trusted, so no human-gate needed."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {"branch": "main"}},
        headers=h,
    )
    assert r.status_code == 200, r.text
    cmd = r.json()
    assert cmd["source_agent"] is None
    assert cmd["dispatch_depth"] == 0
    assert cmd["dispatch_chain_id"]
    assert cmd["approval_state"] == "auto_approved"
    assert cmd["approved_at"] is not None


def test_enqueue_agent_origin_lands_pending_without_rule(client, alice):
    """An agent-driven dispatch with no matching auto-approval rule
    sits at approval_state='pending' — the human-in-the-loop
    default."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {"branch": "main"},
            "source_agent": "polaris",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    cmd = r.json()
    assert cmd["source_agent"] == "polaris"
    assert cmd["approval_state"] == "pending"
    assert cmd["dispatch_depth"] == 0  # chain root


def test_enqueue_inherits_chain_id_and_depth(client, alice):
    """A second dispatch inside the same chain id picks up
    parent_depth + 1."""
    h = auth_headers(alice["api_key"]["plaintext"])
    chain = "11111111-2222-3333-4444-555555555555"
    # Hop 1: polaris -> atlas
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "polaris",
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    assert r.json()["dispatch_depth"] == 0
    # Hop 2: atlas -> hermes (still in the same chain)
    r = client.post(
        "/agents/hermes/commands",
        json={
            "kind": "hermes.post",
            "payload": {"text": "tests passed"},
            "source_agent": "atlas",
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["dispatch_chain_id"] == chain
    assert r.json()["dispatch_depth"] == 1


def test_enqueue_auto_registers_unknown_source_agent(client, alice):
    """First dispatch from a never-seen source registers the agent on
    the fly. Same pattern as the target side — saves a round-trip
    when a new agent comes online and immediately dispatches."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "brand-new-agent",
        },
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["source_agent"] == "brand-new-agent"
    # The new agent shows up in /agents now.
    r = client.get("/agents", headers=h)
    names = [a["name"] for a in r.json()["agents"]]
    assert "brand-new-agent" in names


# ---------- depth cap + daily cap ---------- #


def test_depth_cap_rejects_at_limit(client, alice):
    """When the chain's current depth reaches the source's
    max_dispatch_depth, the next hop returns 422."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Lower polaris's cap so we don't have to chain 5 deep in the test.
    # First make sure polaris exists by enqueueing one command.
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE agents SET max_dispatch_depth = 2 "
                "WHERE workspace_id = :wsid AND name = 'polaris'"
            ),
            {"wsid": alice["workspace"]["id"]},
        )

    chain = "deep-chain-id"
    # depth 0
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "polaris",
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    assert r.json()["dispatch_depth"] == 0
    # depth 1
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "polaris",
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    assert r.json()["dispatch_depth"] == 1
    # depth 2 would equal max (2), so reject.
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "polaris",
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    assert r.status_code == 422
    assert "max_dispatch_depth" in r.json()["detail"]


def test_per_day_cap_rejects_when_exceeded(client, alice):
    """The 24h dispatch count from a source agent is capped via
    Agent.max_dispatch_per_day."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Seed polaris.
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    with session_scope() as s:
        s.execute(
            text(
                "UPDATE agents SET max_dispatch_per_day = 2 "
                "WHERE workspace_id = :wsid AND name = 'polaris'"
            ),
            {"wsid": alice["workspace"]["id"]},
        )
    # The seed dispatch counts as 1 — two more bring us to the limit.
    for i in range(1):
        r = client.post(
            "/agents/atlas/commands",
            json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
            headers=h,
        )
        assert r.status_code == 200
    # Third (= 1 seed + 2 = 3) should hit the cap of 2.
    r = client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    assert r.status_code == 422
    assert "max_dispatch_per_day" in r.json()["detail"]


# ---------- claim: approval-gated ---------- #


def test_claim_skips_pending_approval(client, alice):
    """A command in approval_state='pending' isn't claimable — claim
    returns null until it's approved (or auto_approved at enqueue
    time). Stops agents from acting on commands that haven't been
    sanctioned by a human."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Seed polaris so the source-agent FK passes.
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    # Now an agent-driven dispatch (lands pending).
    client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {"branch": "feature"},
            "source_agent": "polaris",
        },
        headers=h,
    )
    # First commands (the seed and the pending one) — claim should
    # only see the seed (auto-approved, source_agent matches), since
    # the pending one isn't claimable.
    # Seed had source_agent='polaris' too, also lands pending.
    r = client.post("/agents/atlas/commands/claim", headers=h)
    assert r.status_code == 200
    assert r.json()["command"] is None


def test_claim_returns_approved_commands(client, alice):
    """Once a command is approved via the dashboard, claim picks it
    up on the next call."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Seed polaris
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    # Pending command
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {"branch": "abc"},
            "source_agent": "polaris",
        },
        headers=h,
    )
    cmd_id = r.json()["id"]
    # Approve
    h_session = auth_headers(alice["session_token"])
    r = client.post(
        f"/commands/{cmd_id}/approve",
        json={},
        headers=h_session,
    )
    assert r.status_code == 200, r.text
    assert r.json()["approval_state"] == "approved"
    assert r.json()["approved_by_user_id"] == alice["user"]["id"]
    # Now claim picks it up.
    r = client.post("/agents/atlas/commands/claim", headers=h)
    assert r.json()["command"] is not None
    assert r.json()["command"]["id"] == cmd_id


# ---------- approval endpoints ---------- #


def test_approve_idempotency_check(client, alice):
    """Approving an already-approved command is a 400."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # User-initiated → auto_approved at enqueue time
    r = client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}},
        headers=h,
    )
    cmd_id = r.json()["id"]
    h_session = auth_headers(alice["session_token"])
    r = client.post(f"/commands/{cmd_id}/approve", json={}, headers=h_session)
    # Already auto_approved, can't re-approve.
    assert r.status_code == 400


def test_reject_marks_terminal_and_unclaimable(client, alice):
    """Rejecting a pending command flips approval_state to 'rejected'
    and status to 'cancelled' so the worker never tries to run it."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "polaris",
        },
        headers=h,
    )
    cmd_id = r.json()["id"]
    h_session = auth_headers(alice["session_token"])
    r = client.post(
        f"/commands/{cmd_id}/reject",
        json={"reason": "wrong branch"},
        headers=h_session,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["approval_state"] == "rejected"
    assert body["status"] == "cancelled"
    assert "wrong branch" in body["error"]
    # Claim never returns this.
    r = client.post("/agents/atlas/commands/claim", headers=h)
    assert r.json()["command"] is None


# ---------- auto-approval rules ---------- #


def test_auto_approval_rule_exact_match(client, alice):
    """An (exact source, target, kind) rule with mode=auto_approve
    flips the command from pending to auto_approved at enqueue time."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.put(
        "/workspaces/me/auto-approval-rules",
        json={
            "source_agent": "polaris",
            "target_agent": "hermes",
            "command_kind": "hermes.post",
            "mode": "auto_approve",
        },
        headers=h,
    )
    # Seed polaris
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    # Now polaris -> hermes hermes.post should auto-approve.
    r = client.post(
        "/agents/hermes/commands",
        json={
            "kind": "hermes.post",
            "payload": {"text": "ok"},
            "source_agent": "polaris",
        },
        headers=h,
    )
    assert r.json()["approval_state"] == "auto_approved"


def test_auto_approval_rule_wildcard_source(client, alice):
    """A wildcard source (*) lets ANY agent's commands of this kind
    to this target auto-approve. Useful for hermes.post — anyone can
    notify Slack."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.put(
        "/workspaces/me/auto-approval-rules",
        json={
            "source_agent": "*",
            "target_agent": "hermes",
            "command_kind": "hermes.post",
            "mode": "auto_approve",
        },
        headers=h,
    )
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    r = client.post(
        "/agents/hermes/commands",
        json={
            "kind": "hermes.post",
            "payload": {},
            "source_agent": "atlas",
        },
        headers=h,
    )
    assert r.json()["approval_state"] == "auto_approved"


def test_auto_approval_rule_specific_overrides_wildcard(client, alice):
    """An exact (source, target, kind) require_human rule overrides
    a more permissive wildcard. Specific beats general."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Wildcard auto-approve…
    client.put(
        "/workspaces/me/auto-approval-rules",
        json={
            "source_agent": "*",
            "target_agent": "hermes",
            "command_kind": "hermes.post",
            "mode": "auto_approve",
        },
        headers=h,
    )
    # …but specifically require_human for atlas as the source.
    client.put(
        "/workspaces/me/auto-approval-rules",
        json={
            "source_agent": "atlas",
            "target_agent": "hermes",
            "command_kind": "hermes.post",
            "mode": "require_human",
        },
        headers=h,
    )
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h,
    )
    # polaris hits the wildcard auto-approve.
    r = client.post(
        "/agents/hermes/commands",
        json={
            "kind": "hermes.post",
            "payload": {},
            "source_agent": "polaris",
        },
        headers=h,
    )
    assert r.json()["approval_state"] == "auto_approved"
    # atlas hits its specific require_human (precedence beats wildcard).
    r = client.post(
        "/agents/hermes/commands",
        json={
            "kind": "hermes.post",
            "payload": {},
            "source_agent": "atlas",
        },
        headers=h,
    )
    assert r.json()["approval_state"] == "pending"


def test_auto_approval_rule_crud(client, alice):
    """List → upsert → list-shows-it → delete → list-empty."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/workspaces/me/auto-approval-rules", headers=h)
    assert r.json()["rules"] == []

    client.put(
        "/workspaces/me/auto-approval-rules",
        json={
            "source_agent": "polaris",
            "target_agent": "atlas",
            "command_kind": "atlas.run_tests",
            "mode": "auto_approve",
        },
        headers=h,
    )
    r = client.get("/workspaces/me/auto-approval-rules", headers=h)
    rules = r.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["mode"] == "auto_approve"

    # Upsert flips mode in place.
    client.put(
        "/workspaces/me/auto-approval-rules",
        json={
            "source_agent": "polaris",
            "target_agent": "atlas",
            "command_kind": "atlas.run_tests",
            "mode": "require_human",
        },
        headers=h,
    )
    r = client.get("/workspaces/me/auto-approval-rules", headers=h)
    rules = r.json()["rules"]
    assert len(rules) == 1
    assert rules[0]["mode"] == "require_human"

    r = client.delete(
        "/workspaces/me/auto-approval-rules"
        "?source_agent=polaris&target_agent=atlas"
        "&command_kind=atlas.run_tests",
        headers=h,
    )
    assert r.status_code == 200
    r = client.get("/workspaces/me/auto-approval-rules", headers=h)
    assert r.json()["rules"] == []


# ---------- constellation edges ---------- #


def test_constellation_edges_populate_from_dispatches(client, alice):
    """The constellation map's edges array (Phase 11B.3 left empty
    in v1) now fills with (source -> target) pairs from the last 24h
    of dispatched commands."""
    h = auth_headers(alice["api_key"]["plaintext"])
    # Two distinct dispatches: polaris -> atlas and atlas -> hermes.
    client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "polaris",
        },
        headers=h,
    )
    # Then a hop in the same chain.
    client.post(
        "/agents/hermes/commands",
        json={
            "kind": "hermes.post",
            "payload": {"text": "ok"},
            "source_agent": "atlas",
        },
        headers=h,
    )
    # Trigger a run for each agent so the constellation filter keeps
    # them on canvas (it filters dormant non-orchestrator agents).
    for name in ("polaris", "atlas", "hermes"):
        client.post(
            "/events",
            json={
                "kind": "run_started",
                "run_id": f"r-{name}",
                "agent_name": name,
                "payload": {},
            },
            headers=h,
        )

    r = client.get("/workspaces/me/constellation", headers=h)
    body = r.json()
    edge_pairs = {(e["from"], e["to"]) for e in body["edges"]}
    assert ("polaris", "atlas") in edge_pairs
    assert ("atlas", "hermes") in edge_pairs


def test_constellation_edges_workspace_isolated(client, alice, bob):
    """Edges in alice's workspace don't leak into bob's response."""
    h_alice = auth_headers(alice["api_key"]["plaintext"])
    h_bob = auth_headers(bob["api_key"]["plaintext"])
    client.post(
        "/agents/atlas/commands",
        json={"kind": "atlas.run_tests", "payload": {}, "source_agent": "polaris"},
        headers=h_alice,
    )
    r = client.get("/workspaces/me/constellation", headers=h_bob)
    assert r.json()["edges"] == []


# ---------- Phase 11.6: dispatch-chain views ---------- #


def test_list_dispatch_chains_empty_workspace(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get("/workspaces/me/dispatch", headers=h)
    assert r.status_code == 200
    assert r.json() == {"chains": []}


def test_list_dispatch_chains_returns_one_row_per_chain(client, alice):
    """Multiple commands in one chain id collapse to one row in the
    list view; the row carries aggregate metadata (count, max_depth,
    last_activity_at, status)."""
    h = auth_headers(alice["api_key"]["plaintext"])
    chain = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    # Root: user-initiated polaris.evaluate_push (auto-approved).
    r = client.post(
        "/agents/polaris/commands",
        json={
            "kind": "polaris.evaluate_push",
            "payload": {"commit_sha": "abc"},
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    assert r.status_code == 200
    # Hop 1: polaris -> atlas (no rule, lands pending).
    r = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "polaris",
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    assert r.status_code == 200

    r = client.get("/workspaces/me/dispatch", headers=h)
    assert r.status_code == 200
    chains = r.json()["chains"]
    assert len(chains) == 1
    row = chains[0]
    assert row["chain_id"] == chain
    assert row["command_count"] == 2
    assert row["root_agent"] == "polaris"
    assert row["root_kind"] == "polaris.evaluate_push"
    assert row["max_depth"] == 1
    # Atlas command is `pending` approval → chain status surfaces it.
    assert row["status"] == "pending_approval"
    assert row["pending_approval_count"] == 1


def test_list_dispatch_chains_orders_newest_first(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    older_chain = "11111111-2222-3333-4444-555555555555"
    newer_chain = "99999999-8888-7777-6666-555555555555"
    client.post(
        "/agents/polaris/commands",
        json={
            "kind": "polaris.evaluate_push",
            "payload": {},
            "dispatch_chain_id": older_chain,
        },
        headers=h,
    )
    client.post(
        "/agents/polaris/commands",
        json={
            "kind": "polaris.evaluate_push",
            "payload": {},
            "dispatch_chain_id": newer_chain,
        },
        headers=h,
    )
    chains = client.get("/workspaces/me/dispatch", headers=h).json()["chains"]
    assert [c["chain_id"] for c in chains] == [newer_chain, older_chain]


def test_list_dispatch_chains_workspace_isolated(client, alice, bob):
    h_a = auth_headers(alice["api_key"]["plaintext"])
    h_b = auth_headers(bob["api_key"]["plaintext"])
    client.post(
        "/agents/polaris/commands",
        json={"kind": "polaris.evaluate_push", "payload": {}},
        headers=h_a,
    )
    assert client.get("/workspaces/me/dispatch", headers=h_b).json()["chains"] == []


def test_get_dispatch_chain_returns_full_command_list(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    chain = "abcdef00-0000-0000-0000-000000000001"
    client.post(
        "/agents/polaris/commands",
        json={
            "kind": "polaris.evaluate_push",
            "payload": {"commit_sha": "abc"},
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "source_agent": "polaris",
            "dispatch_chain_id": chain,
        },
        headers=h,
    )
    r = client.get(f"/workspaces/me/dispatch/{chain}", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["chain_id"] == chain
    assert len(body["commands"]) == 2
    # Ordered by depth (root first), then created_at.
    assert body["commands"][0]["dispatch_depth"] == 0
    assert body["commands"][0]["agent_name"] == "polaris"
    assert body["commands"][1]["dispatch_depth"] == 1
    assert body["commands"][1]["agent_name"] == "atlas"
    assert body["events"] == []  # no agents have emitted yet


def test_get_dispatch_chain_unknown_id_404s(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.get(
        "/workspaces/me/dispatch/00000000-0000-0000-0000-000000000abc",
        headers=h,
    )
    assert r.status_code == 404


def test_get_dispatch_chain_workspace_isolated(client, alice, bob):
    """Bob can't peek at alice's chains by guessing the id."""
    h_a = auth_headers(alice["api_key"]["plaintext"])
    h_b = auth_headers(bob["api_key"]["plaintext"])
    chain = "deadbeef-0000-0000-0000-000000000000"
    client.post(
        "/agents/polaris/commands",
        json={
            "kind": "polaris.evaluate_push",
            "payload": {},
            "dispatch_chain_id": chain,
        },
        headers=h_a,
    )
    r = client.get(f"/workspaces/me/dispatch/{chain}", headers=h_b)
    assert r.status_code == 404


def test_get_dispatch_chain_includes_linked_events(client, alice):
    """Events whose payload.command_id matches a chain command appear
    in the timeline. Used by the dashboard to show 'atlas.tests_run'
    inline under the atlas.run_tests command that produced it."""
    h = auth_headers(alice["api_key"]["plaintext"])
    chain = "00000000-1234-5678-9abc-def000000000"
    cmd = client.post(
        "/agents/atlas/commands",
        json={
            "kind": "atlas.run_tests",
            "payload": {},
            "dispatch_chain_id": chain,
        },
        headers=h,
    ).json()
    cmd_id = cmd["id"]
    # Atlas would emit atlas.tests_run with command_id in payload.
    client.post(
        "/events",
        json={
            "agent_name": "atlas",
            "kind": "atlas.tests_run",
            "run_id": "run-1",
            "payload": {
                "command_id": cmd_id,
                "passed": 10,
                "failed": 0,
            },
        },
        headers=h,
    )
    r = client.get(f"/workspaces/me/dispatch/{chain}", headers=h)
    body = r.json()
    assert len(body["events"]) == 1
    assert body["events"][0]["kind"] == "atlas.tests_run"
    assert body["events"][0]["command_id"] == cmd_id
