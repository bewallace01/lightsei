"""Phase 14.3: tests for backend/eval_runner.py.

Two surfaces under test:

1. `run_eval_job` — the dispatch handler. Pre-checks (anthropic key,
   budget cap), sampling, judge calls (stubbed), persisting
   RunEvaluation rows, recording cost on lightsei.system, robustness
   to per-sample failures.

2. The cron-style enqueuer — one `eval_runs` row per workspace per
   cycle, interval read from env with bad-value fallback.

Anthropic is stubbed via the same `_FakeClient` / `_fake_response`
shape `test_agent_generator.py` uses. No real LLM calls; CI doesn't
need an API key.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

import eval_runner
import eval_sampler
from db import session_scope
from models import Agent, Event, Run, RunEvaluation, WorkspaceSecret
from tests.conftest import auth_headers


# ---------- Helpers ---------- #


def _ensure_agent(
    session, workspace_id, *, name, role="executor", system_prompt=None,
    model=None,
):
    now = datetime.now(timezone.utc)
    session.add(
        Agent(
            workspace_id=workspace_id,
            name=name,
            role=role,
            system_prompt=system_prompt,
            model=model,
            created_at=now,
            updated_at=now,
        )
    )


def _make_run(session, workspace_id, *, agent_name, started_at, ended_at,
              run_id=None):
    if run_id is None:
        run_id = str(uuid.uuid4())
    session.add(
        Run(
            id=run_id,
            workspace_id=workspace_id,
            agent_name=agent_name,
            started_at=started_at,
            ended_at=ended_at,
            cost_usd=Decimal("0"),
        )
    )
    return run_id


def _add_event(session, workspace_id, *, run_id, agent_name, kind, payload,
               timestamp):
    session.add(
        Event(
            workspace_id=workspace_id,
            run_id=run_id,
            agent_name=agent_name,
            kind=kind,
            payload=payload,
            timestamp=timestamp,
        )
    )


def _seed_anthropic_secret(client, api_key, value="sk-ant-fake"):
    r = client.put(
        "/workspaces/me/secrets/ANTHROPIC_API_KEY",
        headers=auth_headers(api_key),
        json={"value": value},
    )
    assert r.status_code == 200, r.text


class _FakeUsage:
    def __init__(self, in_t, out_t):
        self.input_tokens = in_t
        self.output_tokens = out_t


def _fake_verdict_response(
    *, verdict="good", reasons=None, confidence=0.9,
    model=eval_sampler.JUDGE_MODEL, tokens_in=200, tokens_out=80,
):
    """Build the messages.create() return value the handler expects."""
    if reasons is None:
        reasons = ["on-task", "well-formatted"]
    tool_block = SimpleNamespace(
        type="tool_use",
        name="submit_verdict",
        input={
            "verdict": verdict,
            "reasons": reasons,
            "confidence": confidence,
        },
    )
    return SimpleNamespace(
        content=[tool_block],
        stop_reason="tool_use",
        model=model,
        usage=_FakeUsage(tokens_in, tokens_out),
    )


class _FakeClient:
    """Stand-in for `anthropic.Anthropic`. Tests overwrite
    `.messages.create` per scenario."""

    def __init__(self, *args, **kwargs):
        self.messages = SimpleNamespace(create=lambda **kw: None)


def _seed_judgeable_run(session, workspace_id, *, agent_name, run_id, now):
    """One completed run with a plan + output event so the judge prompt
    has something meaningful to look at."""
    _make_run(
        session, workspace_id,
        agent_name=agent_name, run_id=run_id,
        started_at=now - timedelta(minutes=5),
        ended_at=now - timedelta(minutes=2),
    )
    _add_event(
        session, workspace_id, run_id=run_id, agent_name=agent_name,
        kind="run_started", payload={},
        timestamp=now - timedelta(minutes=5),
    )
    _add_event(
        session, workspace_id, run_id=run_id, agent_name=agent_name,
        kind=f"{agent_name}.tick", payload={"description": "did the thing"},
        timestamp=now - timedelta(minutes=4),
    )
    _add_event(
        session, workspace_id, run_id=run_id, agent_name=agent_name,
        kind="run_ended", payload={},
        timestamp=now - timedelta(minutes=2),
    )


# ---------- run_eval_job: pre-checks ---------- #


def test_run_eval_job_skips_when_no_anthropic_key(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        result = eval_runner.run_eval_job(s, workspace_id, {})
    assert result["skipped_reason"] == "no_anthropic_key"
    assert result["evaluated"] == 0
    assert result["sampled"] == 0


def test_run_eval_job_skips_when_over_budget(client, alice, monkeypatch):
    workspace_id = alice["workspace"]["id"]
    _seed_anthropic_secret(client, alice["api_key"]["plaintext"])
    # Set a $0.01 cap and pretend $0.05 has been spent.
    with session_scope() as s:
        s.execute(
            text("UPDATE workspaces SET budget_usd_monthly = 0.01 WHERE id = :ws"),
            {"ws": workspace_id},
        )

    def fake_mtd(session, ws_id):
        return {"total_usd": 0.05}

    monkeypatch.setattr(eval_runner, "workspace_cost_mtd", fake_mtd)

    with session_scope() as s:
        result = eval_runner.run_eval_job(s, workspace_id, {})
    assert result["skipped_reason"].startswith("over_budget")
    assert result["evaluated"] == 0


def test_run_eval_job_returns_no_samples_when_pool_is_empty(client, alice):
    workspace_id = alice["workspace"]["id"]
    _seed_anthropic_secret(client, alice["api_key"]["plaintext"])
    with session_scope() as s:
        result = eval_runner.run_eval_job(s, workspace_id, {})
    assert result["skipped_reason"] == "no_samples"
    assert result["evaluated"] == 0


# ---------- run_eval_job: happy path ---------- #


def test_run_eval_job_writes_verdict_row(client, alice, monkeypatch):
    """Sample one run, judge it (stubbed), assert a RunEvaluation
    row landed with the right shape."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _seed_anthropic_secret(client, api_key)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus", system_prompt="be careful")
        _seed_judgeable_run(
            s, workspace_id, agent_name="argus", run_id="argus-r1", now=now,
        )

    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_verdict_response(
        verdict="good", reasons=["scan was thorough"], confidence=0.85,
    )
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with session_scope() as s:
        result = eval_runner.run_eval_job(s, workspace_id, {})

    assert result["sampled"] == 1
    assert result["evaluated"] == 1
    assert result["errored"] == 0
    assert result["tokens_in"] == 200
    assert result["tokens_out"] == 80

    with session_scope() as s:
        rows = s.execute(
            select(RunEvaluation).where(RunEvaluation.run_id == "argus-r1")
        ).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.workspace_id == workspace_id
    assert row.agent_name == "argus"
    assert row.judge_model == eval_sampler.JUDGE_MODEL
    assert row.verdict == "good"
    assert row.reasons == ["scan was thorough"]
    assert float(row.confidence) == pytest.approx(0.85, abs=1e-3)
    assert row.judge_tokens_in == 200
    assert row.judge_tokens_out == 80
    assert float(row.judge_cost_usd) > 0


def test_run_eval_job_records_cost_on_lightsei_system(client, alice, monkeypatch):
    """Phase 12D rollup parity: judge spend must land on
    lightsei.system so /cost reflects it the same way generation
    spend does."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _seed_anthropic_secret(client, api_key)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _seed_judgeable_run(
            s, workspace_id, agent_name="argus", run_id="argus-r1", now=now,
        )

    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_verdict_response(
        tokens_in=1000, tokens_out=500,
    )
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with session_scope() as s:
        eval_runner.run_eval_job(s, workspace_id, {})

    cost = client.get(
        "/workspaces/me/cost", headers=auth_headers(api_key)
    ).json()
    by_agent = {a["agent_name"]: a for a in cost["by_agent"]}
    assert "lightsei.system" in by_agent, (
        f"judge cost not attributed; saw {list(by_agent)}"
    )
    assert by_agent["lightsei.system"]["mtd_usd"] > 0


def test_run_eval_job_handles_multiple_samples(client, alice, monkeypatch):
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _seed_anthropic_secret(client, api_key)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _ensure_agent(s, workspace_id, name="vela")
        _seed_judgeable_run(
            s, workspace_id, agent_name="argus", run_id="argus-r1", now=now,
        )
        _seed_judgeable_run(
            s, workspace_id, agent_name="vela", run_id="vela-r1", now=now,
        )

    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_verdict_response()
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with session_scope() as s:
        result = eval_runner.run_eval_job(s, workspace_id, {})

    assert result["sampled"] == 2
    assert result["evaluated"] == 2


# ---------- run_eval_job: per-sample resilience ---------- #


def test_run_eval_job_continues_after_one_anthropic_error(client, alice, monkeypatch):
    """One bad judge call shouldn't stop the cycle. errored counter
    bumps; remaining samples still get evaluated."""
    import anthropic
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _seed_anthropic_secret(client, api_key)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _ensure_agent(s, workspace_id, name="vela")
        _seed_judgeable_run(
            s, workspace_id, agent_name="argus", run_id="argus-r1", now=now,
        )
        _seed_judgeable_run(
            s, workspace_id, agent_name="vela", run_id="vela-r1", now=now,
        )

    class _Boom(anthropic.APIError):
        def __init__(self):
            self.status_code = 500
            self.message = "Internal error"
        def __str__(self):
            return "Internal error"

    calls = {"n": 0}

    def fake_create(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Boom()
        return _fake_verdict_response()

    fake = _FakeClient()
    fake.messages.create = fake_create
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with session_scope() as s:
        result = eval_runner.run_eval_job(s, workspace_id, {})

    assert result["sampled"] == 2
    assert result["evaluated"] == 1
    assert result["errored"] == 1


def test_run_eval_job_handles_non_tool_response(client, alice, monkeypatch):
    """Judge returns plain text instead of calling submit_verdict.
    The handler must error out for that sample (not default to good)
    and continue."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _seed_anthropic_secret(client, api_key)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _seed_judgeable_run(
            s, workspace_id, agent_name="argus", run_id="argus-r1", now=now,
        )

    fake = _FakeClient()
    fake.messages.create = lambda **kw: SimpleNamespace(
        content=[SimpleNamespace(type="text", text="I think it was fine.")],
        stop_reason="end_turn",
        model=eval_sampler.JUDGE_MODEL,
        usage=_FakeUsage(100, 20),
    )
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with session_scope() as s:
        result = eval_runner.run_eval_job(s, workspace_id, {})

    assert result["evaluated"] == 0
    assert result["errored"] == 1

    # Confirm no RunEvaluation row was inserted for the unparseable
    # response — defaulting to 'good' here would silently pollute the
    # quality signal.
    with session_scope() as s:
        rows = s.execute(select(RunEvaluation)).scalars().all()
    assert rows == []


def test_run_eval_job_skips_already_evaluated_runs(client, alice, monkeypatch):
    """Sampler already filters via NOT EXISTS, but cross-check that
    repeated handler invocations don't double-write rows."""
    workspace_id = alice["workspace"]["id"]
    api_key = alice["api_key"]["plaintext"]
    _seed_anthropic_secret(client, api_key)

    now = datetime.now(timezone.utc)
    with session_scope() as s:
        _ensure_agent(s, workspace_id, name="argus")
        _seed_judgeable_run(
            s, workspace_id, agent_name="argus", run_id="argus-r1", now=now,
        )

    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_verdict_response()
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with session_scope() as s:
        first = eval_runner.run_eval_job(s, workspace_id, {})
    with session_scope() as s:
        second = eval_runner.run_eval_job(s, workspace_id, {})

    assert first["evaluated"] == 1
    assert second["evaluated"] == 0
    assert second["skipped_reason"] == "no_samples"

    with session_scope() as s:
        rows = s.execute(select(RunEvaluation)).scalars().all()
    assert len(rows) == 1


# ---------- Cron enqueuer ---------- #


def test_eval_interval_reads_env_with_fallback(monkeypatch):
    monkeypatch.setenv("LIGHTSEI_EVAL_INTERVAL_S", "120")
    assert eval_runner._eval_interval_s() == 120.0
    monkeypatch.setenv("LIGHTSEI_EVAL_INTERVAL_S", "garbage")
    assert eval_runner._eval_interval_s() == float(eval_runner.DEFAULT_EVAL_INTERVAL_S)
    # Floor at 10s so a misconfigured tiny interval can't hammer.
    monkeypatch.setenv("LIGHTSEI_EVAL_INTERVAL_S", "1")
    assert eval_runner._eval_interval_s() == 10.0


def test_enqueue_eval_job_for_workspace_drops_pending_row(client, alice):
    workspace_id = alice["workspace"]["id"]
    with session_scope() as s:
        job_id = eval_runner.enqueue_eval_job_for_workspace(
            s, workspace_id=workspace_id,
        )
        s.commit()

    # Verify the row landed with kind='eval_runs' for this workspace. The
    # in-process jobs runner may have already claimed it (pending -> running/
    # terminal), so don't pin the status — the point is enqueue created the row.
    with session_scope() as s:
        row = s.execute(
            text(
                "SELECT kind, status, workspace_id FROM generation_jobs "
                "WHERE id = :id"
            ),
            {"id": job_id},
        ).mappings().first()
    assert row is not None
    assert row["kind"] == "eval_runs"
    assert row["workspace_id"] == workspace_id


def test_cron_enqueues_one_job_per_workspace(client, alice, bob):
    """The cron sweeps all workspaces; assert it drops exactly one
    eval_runs row per workspace it sees."""
    n = eval_runner._enqueue_eval_jobs_for_all_workspaces()
    # Both alice and bob exist as workspaces; cron picks them up.
    assert n >= 2

    # Count rows regardless of status: the in-process jobs runner may have
    # already claimed the freshly enqueued pending rows (pending -> running/
    # terminal). What matters is exactly one eval_runs row landed per workspace.
    with session_scope() as s:
        per_ws = s.execute(
            text(
                "SELECT workspace_id, count(*) AS n FROM generation_jobs "
                "WHERE kind = 'eval_runs' "
                "GROUP BY workspace_id"
            )
        ).mappings().all()
    counts = {r["workspace_id"]: r["n"] for r in per_ws}
    assert counts.get(alice["workspace"]["id"]) == 1
    assert counts.get(bob["workspace"]["id"]) == 1


# ---------- Handler is registered ---------- #


def test_eval_runs_handler_is_registered(client, alice):
    """The runner's dispatch table must include 'eval_runs' or every
    cron-enqueued job will fail with no_handler. Defensive check
    against forgetting to wire `_register()` in eval_runner.py."""
    import jobs as jobs_mod
    # Ensure default handlers are loaded (start_runner does this in prod;
    # tests don't always go through that path).
    jobs_mod._load_default_handlers()
    assert "eval_runs" in jobs_mod._HANDLERS
