"""Phase 11B.1: cost telemetry — runs.cost_usd, model_pricing seed,
GET /workspaces/me/cost, workspaces.budget_usd_monthly patch.

Covers the home-page cost panel's data path. The runtime SDK (handled
in sdk/tests/) and the dashboard (handled in dashboard/) sit on top
of this surface; here we exercise the backend in isolation.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import text

from db import session_scope
from models import ModelPricing, Run, Workspace
from tests.conftest import auth_headers


def test_model_pricing_seed_writes_known_models(client):
    """Boot path's `seed_model_pricing` is idempotent and lays down
    the in-code PRICING dict. The `client` fixture's TestClient
    context manager fires FastAPI's startup hook, which re-seeds the
    table — important because the autouse truncate fixture wipes it
    between tests."""
    from pricing import PRICING

    with session_scope() as s:
        rows = s.query(ModelPricing).all()

    seeded = {(r.provider, r.model) for r in rows}
    # Spot-check a couple from each provider — exhaustive parity is
    # less interesting than "the seed actually ran."
    assert ("anthropic", "claude-opus-4-7") in seeded
    assert ("anthropic", "claude-haiku-4-5") in seeded
    assert ("openai", "gpt-4o") in seeded
    assert ("openai", "gpt-4o-mini") in seeded
    # Every model in PRICING shows up in the table.
    pricing_models = {model for model in PRICING.keys()}
    seeded_models = {model for _, model in seeded}
    assert pricing_models <= seeded_models


def test_event_ingest_increments_run_cost(client, alice):
    """Each `llm_call_completed` event posted to /events should bump
    the run row's cached `cost_usd` by the price implied by its
    payload."""
    h = auth_headers(alice["api_key"]["plaintext"])

    # 1000 input tokens + 200 output tokens of claude-haiku-4-5
    # at ($0.80, $4.00) per 1M = (1000 * 0.80 + 200 * 4.00) / 1e6
    # = (800 + 800) / 1e6 = 0.0016 USD
    expected_first = (1000 * 0.80 + 200 * 4.00) / 1_000_000
    payload_a = {
        "model": "claude-haiku-4-5",
        "input_tokens": 1000,
        "output_tokens": 200,
    }
    r = client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "run-cost-1",
            "agent_name": "atlas",
            "payload": payload_a,
        },
        headers=h,
    )
    assert r.status_code == 200, r.text

    with session_scope() as s:
        run = s.get(Run, "run-cost-1")
        assert run is not None
        assert abs(float(run.cost_usd) - expected_first) < 1e-6

    # Second event: another 500 in, 100 out — same model.
    payload_b = {
        "model": "claude-haiku-4-5",
        "input_tokens": 500,
        "output_tokens": 100,
    }
    expected_second = expected_first + (500 * 0.80 + 100 * 4.00) / 1_000_000
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "run-cost-1",
            "agent_name": "atlas",
            "payload": payload_b,
        },
        headers=h,
    )
    with session_scope() as s:
        run = s.get(Run, "run-cost-1")
        assert abs(float(run.cost_usd) - expected_second) < 1e-6


def test_unknown_model_contributes_zero(client, alice):
    """Unknown models in `llm_call_completed` payloads must not crash
    the ingest path and must contribute 0 to cost_usd — matches
    `compute_cost_usd`'s default-to-zero policy."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "run-unknown-model",
            "agent_name": "atlas",
            "payload": {
                "model": "some-future-model-not-in-pricing",
                "input_tokens": 9999,
                "output_tokens": 9999,
            },
        },
        headers=h,
    )
    with session_scope() as s:
        run = s.get(Run, "run-unknown-model")
        assert run is not None
        assert float(run.cost_usd) == 0.0


def test_workspace_cost_endpoint_aggregates_mtd(client, alice):
    """`GET /workspaces/me/cost` surfaces MTD spend, projected EOM,
    per-agent + per-model breakdown, and the budget bar state."""
    h = auth_headers(alice["api_key"]["plaintext"])

    # Two runs from two agents, both this month, on different models.
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "polaris-r1",
            "agent_name": "polaris",
            "payload": {
                "model": "claude-opus-4-7",
                "input_tokens": 10000,
                "output_tokens": 1000,
            },
        },
        headers=h,
    )
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "atlas-r1",
            "agent_name": "atlas",
            "payload": {
                "model": "claude-haiku-4-5",
                "input_tokens": 2000,
                "output_tokens": 500,
            },
        },
        headers=h,
    )

    expected_polaris = (10000 * 15.00 + 1000 * 75.00) / 1_000_000
    expected_atlas = (2000 * 0.80 + 500 * 4.00) / 1_000_000
    expected_mtd = expected_polaris + expected_atlas

    r = client.get("/workspaces/me/cost", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()

    assert abs(body["mtd_usd"] - expected_mtd) < 1e-6
    # Both agents present, sorted by cost desc.
    agent_names = [a["agent_name"] for a in body["by_agent"]]
    assert agent_names == ["polaris", "atlas"]
    assert abs(body["by_agent"][0]["mtd_usd"] - expected_polaris) < 1e-6
    assert abs(body["by_agent"][1]["mtd_usd"] - expected_atlas) < 1e-6
    # by_model breakdown matches the same rollup.
    by_model = {m["model"]: m for m in body["by_model"]}
    assert "claude-opus-4-7" in by_model
    assert "claude-haiku-4-5" in by_model
    assert abs(
        by_model["claude-opus-4-7"]["mtd_usd"] - expected_polaris
    ) < 1e-6
    # No budget set -> null fields.
    assert body["budget_usd_monthly"] is None
    assert body["budget_used_pct"] is None
    # Projection >= MTD (linear extrapolation never reduces).
    assert body["projected_eom_usd"] >= body["mtd_usd"] - 1e-6


def test_workspace_cost_isolates_across_workspaces(client, alice, bob):
    """Spend in alice's workspace doesn't leak into bob's totals."""
    h_alice = auth_headers(alice["api_key"]["plaintext"])
    h_bob = auth_headers(bob["api_key"]["plaintext"])

    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "alice-run",
            "agent_name": "polaris",
            "payload": {
                "model": "claude-opus-4-7",
                "input_tokens": 5000,
                "output_tokens": 500,
            },
        },
        headers=h_alice,
    )
    r = client.get("/workspaces/me/cost", headers=h_bob)
    assert r.status_code == 200
    body = r.json()
    assert body["mtd_usd"] == 0.0
    assert body["by_agent"] == []


def test_workspace_budget_patch_round_trips(client, alice):
    """PATCH /workspaces/me with budget_usd_monthly persists, and the
    cost endpoint surfaces the cap + used percentage."""
    h = auth_headers(alice["api_key"]["plaintext"])

    r = client.patch(
        "/workspaces/me",
        json={"budget_usd_monthly": 25.00},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["budget_usd_monthly"] == 25.00

    # Add some spend.
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "budget-r1",
            "agent_name": "polaris",
            "payload": {
                "model": "claude-opus-4-7",
                "input_tokens": 10000,
                "output_tokens": 1000,
            },
        },
        headers=h,
    )
    r = client.get("/workspaces/me/cost", headers=h)
    body = r.json()
    assert body["budget_usd_monthly"] == 25.00
    assert body["budget_used_pct"] is not None
    assert 0 < body["budget_used_pct"] < 100

    # Explicit null clears the cap.
    r = client.patch(
        "/workspaces/me",
        json={"budget_usd_monthly": None},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["budget_usd_monthly"] is None

    r = client.get("/workspaces/me/cost", headers=h)
    body = r.json()
    assert body["budget_usd_monthly"] is None
    assert body["budget_used_pct"] is None


def test_workspace_budget_rejects_negative(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = client.patch(
        "/workspaces/me",
        json={"budget_usd_monthly": -1.0},
        headers=h,
    )
    assert r.status_code == 400


def test_patch_name_alone_doesnt_clear_budget(client, alice):
    """A patch that only sends `name` must not erase a previously-set
    `budget_usd_monthly`. model_fields_set is what makes this work."""
    h = auth_headers(alice["api_key"]["plaintext"])

    client.patch(
        "/workspaces/me",
        json={"budget_usd_monthly": 50.00},
        headers=h,
    )
    r = client.patch(
        "/workspaces/me",
        json={"name": "renamed"},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "renamed"
    assert body["budget_usd_monthly"] == 50.00


def test_backfill_populates_existing_runs(client, alice):
    """Phase 11B.1 ships a SQL backfill on migration. Simulate the
    pre-migration state by writing a run row with cost_usd=0 + an
    llm_call_completed event for it, then run the backfill SQL by hand
    and confirm the column reflects the events.

    (We can't reverse the running migration in-test; the test database
    is post-migration. Instead we set cost_usd=0 explicitly to simulate
    a pre-Phase-11B.1 row, run the same UPDATE the migration runs, and
    verify it matches the live ingest path's calculation.)
    """
    h = auth_headers(alice["api_key"]["plaintext"])
    client.post(
        "/events",
        json={
            "kind": "llm_call_completed",
            "run_id": "backfill-r1",
            "agent_name": "polaris",
            "payload": {
                "model": "claude-sonnet-4-6",
                "input_tokens": 5000,
                "output_tokens": 1000,
            },
        },
        headers=h,
    )
    expected = (5000 * 3.00 + 1000 * 15.00) / 1_000_000

    # Force pre-backfill state, then run the migration's UPDATE verbatim.
    with session_scope() as s:
        s.execute(
            text("UPDATE runs SET cost_usd = 0 WHERE id = :id"),
            {"id": "backfill-r1"},
        )
    with session_scope() as s:
        s.execute(
            text(
                """
                UPDATE runs r
                SET cost_usd = COALESCE(sub.cost, 0)
                FROM (
                    SELECT
                        e.run_id,
                        SUM(
                            COALESCE((e.payload->>'input_tokens')::numeric, 0)
                              * COALESCE(mp.input_per_million_usd, 0) / 1000000.0
                            +
                            COALESCE((e.payload->>'output_tokens')::numeric, 0)
                              * COALESCE(mp.output_per_million_usd, 0) / 1000000.0
                        ) AS cost
                    FROM events e
                    LEFT JOIN model_pricing mp
                      ON mp.model = e.payload->>'model'
                    WHERE e.kind = 'llm_call_completed'
                    GROUP BY e.run_id
                ) sub
                WHERE r.id = sub.run_id
                """
            )
        )
    with session_scope() as s:
        run = s.get(Run, "backfill-r1")
        assert run is not None
        assert abs(float(run.cost_usd) - expected) < 1e-6
