"""Phase 12C.1: project-analysis endpoint.

Two surfaces under test:

1. The pure `team_planner` module — submit_team schema, prompt
   building, plan validation. No HTTP, no Anthropic, no DB.
2. `POST /workspaces/me/teams/plan` with a stubbed Anthropic client.
   Mirrors `test_agent_generator.py`'s _FakeClient pattern so CI
   doesn't need a real API key.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from team_planner import (
    MAX_DISPATCHES_PER_BOT,
    MAX_TEAM_SIZE,
    MIN_TEAM_SIZE,
    SUBMIT_TEAM_TOOL,
    build_system_prompt,
    build_user_message,
    run_team_plan_job,
    validate_team_plan,
)
from db import SessionLocal
from models import Run
from tests.conftest import (
    GenerationJobFailed,
    auth_headers,
    kick_and_wait_for_job,
)


# ---------- Pure-module unit tests ---------- #


def test_submit_team_schema_has_required_fields():
    assert SUBMIT_TEAM_TOOL["name"] == "submit_team"
    assert SUBMIT_TEAM_TOOL["strict"] is True
    schema = SUBMIT_TEAM_TOOL["input_schema"]
    assert set(schema["required"]) == {"rationale", "team"}
    team = schema["properties"]["team"]
    # Anthropic's strict mode rejects array `minItems`/`maxItems` values
    # other than 0 or 1 — we used to declare them on the schema but had
    # to relax them. The bounds are still enforced by validate_team_plan
    # (with a corrective retry turn); the description text tells Claude
    # the target range up front so violations are rare.
    assert "minItems" not in team
    assert "maxItems" not in team
    assert str(MIN_TEAM_SIZE) in team["description"]
    assert str(MAX_TEAM_SIZE) in team["description"]
    # Per-bot tool description also surfaces the dispatch cap to the LLM.
    assert str(MAX_DISPATCHES_PER_BOT) in SUBMIT_TEAM_TOOL["description"]
    member = team["items"]
    assert set(member["required"]) == {
        "name", "role", "sensitivity_hint", "summary", "command_kinds",
        "dispatches_to", "needs_workspace_secrets", "capabilities",
        "draft_description",
    }
    # role is enum-constrained
    assert set(member["properties"]["role"]["enum"]) == {
        "orchestrator", "specialist", "messenger",
    }
    # P16.x: sensitivity_hint must be one of the four trust-zone levels.
    # The Compliance preset uses this to assign zones rather than
    # role-based defaults.
    assert set(member["properties"]["sensitivity_hint"]["enum"]) == {
        "public", "internal", "sensitive", "pii",
    }
    # Dispatch cap is described in prose (validator enforces; tool
    # schema can't carry `maxItems > 1` in strict mode).
    assert "maxItems" not in member["properties"]["dispatches_to"]
    assert (
        str(MAX_DISPATCHES_PER_BOT)
        in member["properties"]["dispatches_to"]["description"]
    )


def test_build_system_prompt_includes_existing_agents_and_filters_dictionary():
    existing = [
        {"name": "polaris", "role": "orchestrator", "command_kinds": []},
        {"name": "atlas", "role": "specialist", "command_kinds": ["atlas.run_tests"]},
    ]
    prompt = build_system_prompt(
        existing_agents=existing, reserved_names=set(),
    )
    # Existing agents section
    assert "polaris" in prompt
    assert "atlas.run_tests" in prompt
    # Reserved names auto-filtered from the star table — polaris and
    # atlas should NOT appear in the dictionary section.
    dict_section = prompt.split("Star-naming dictionary")[1]
    assert "`polaris`" not in dict_section
    assert "`atlas`" not in dict_section
    # But other star names should still be in the dictionary.
    assert "`vega`" in dict_section
    assert "`hermes`" in dict_section


def test_build_user_message_includes_all_inputs():
    msg = build_user_message(
        readme_text="My project README",
        freeform_description="extra context",
        github_repo="bewallace01/lightsei",
    )
    assert "My project README" in msg
    assert "extra context" in msg
    assert "bewallace01/lightsei" in msg


def test_build_user_message_handles_missing_inputs():
    msg = build_user_message(
        readme_text="just the readme",
        freeform_description=None,
        github_repo=None,
    )
    assert "just the readme" in msg
    assert "Additional context" not in msg


def _team_member(
    name: str = "vega",
    role: str = "specialist",
    dispatches_to: list | None = None,
    command_kinds: list | None = None,
    sensitivity_hint: str = "internal",
    capabilities: list | None = None,
) -> dict:
    return {
        "name": name,
        "role": role,
        "sensitivity_hint": sensitivity_hint,
        "summary": f"{name} does the thing",
        "command_kinds": command_kinds if command_kinds is not None else [f"{name}.do"],
        "dispatches_to": dispatches_to if dispatches_to is not None else [],
        "needs_workspace_secrets": [],
        # Phase 24.1: capabilities is required + validated against
        # KNOWN_CAPABILITIES + connector:* prefix. Default to []
        # (valid: operator-only bot) so existing tests don't break.
        "capabilities": capabilities if capabilities is not None else [],
        "draft_description": f"A bot named {name} that does things.",
    }


def test_validate_team_plan_passes_clean_plan():
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega"),
            _team_member("argus", dispatches_to=["hermes"]),
            _team_member("hermes", role="messenger", command_kinds=["hermes.post"]),
        ],
    }
    assert validate_team_plan(plan, reserved_names=set()) == []


def test_validate_team_plan_rejects_off_dictionary_name():
    plan = {
        "rationale": "test",
        "team": [
            _team_member("invented-star"),
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    assert any("invented-star" in p for p in problems)


def test_validate_team_plan_rejects_reserved_name():
    plan = {
        "rationale": "test",
        "team": [
            _team_member("polaris"),  # already in the workspace
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names={"polaris"})
    assert any("already in use" in p for p in problems)


def test_validate_team_plan_rejects_duplicate_within_team():
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega"),
            _team_member("vega"),  # duplicate
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    assert any("used twice" in p for p in problems)


def test_validate_team_plan_rejects_size_violation():
    plan_short = {
        "rationale": "test",
        "team": [_team_member("vega"), _team_member("argus")],  # 2 < min 3
    }
    assert any("between" in p for p in validate_team_plan(plan_short, set()))

    plan_long = {
        "rationale": "test",
        "team": [
            _team_member(name)
            for name in [
                "vega", "argus", "hermes", "atlas", "sirius",
                "rigel", "altair", "lyra",
            ]  # 8 > max 7
        ],
    }
    assert any("between" in p for p in validate_team_plan(plan_long, set()))


def test_validate_team_plan_rejects_dangling_dispatch_edge():
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega", dispatches_to=["does-not-exist"]),
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    assert any("does-not-exist" in p for p in problems)


def test_validate_team_plan_accepts_existing_agent_as_dispatch_target():
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega", dispatches_to=["polaris"]),  # existing
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    assert validate_team_plan(plan, reserved_names={"polaris"}) == []


def test_validate_team_plan_rejects_two_orchestrators():
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega", role="orchestrator"),
            _team_member("argus", role="orchestrator"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    assert any("orchestrator" in p for p in problems)


# ---------- P16.x: sensitivity_hint validation ---------- #


def test_validate_team_plan_rejects_missing_sensitivity_hint():
    """Bot without sensitivity_hint → flagged in the corrective retry
    message so the LLM tries again with a hint."""
    plan = {
        "rationale": "test",
        "team": [
            {k: v for k, v in _team_member("vega").items() if k != "sensitivity_hint"},
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    assert any("sensitivity_hint" in p for p in problems)


def test_validate_team_plan_rejects_invalid_sensitivity_hint():
    """Hint must be one of the four levels; anything else fails."""
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega", sensitivity_hint="ultra-secret"),
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    assert any("sensitivity_hint" in p for p in problems)


def test_validate_team_plan_accepts_all_four_hint_values():
    """All four levels are valid; the validator doesn't care which one
    a bot picked (that's a per-bot judgment call)."""
    for hint in ("public", "internal", "sensitive", "pii"):
        plan = {
            "rationale": "test",
            "team": [
                _team_member("vega", sensitivity_hint=hint),
                _team_member("argus"),
                _team_member("hermes", role="messenger"),
            ],
        }
        problems = validate_team_plan(plan, reserved_names=set())
        assert problems == [], f"hint {hint!r} produced problems: {problems}"


# ---------- Phase 24.1: capabilities validation ---------- #


def test_validate_team_plan_accepts_empty_capabilities():
    """Empty capability list is valid — 'operator-only bot, has no
    outbound powers yet.' Default from the helper; existing tests
    rely on this implicitly."""
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega", capabilities=[]),
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    assert validate_team_plan(plan, reserved_names=set()) == []


def test_validate_team_plan_accepts_known_capabilities():
    """Each entry from the well-known vocabulary passes."""
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega", capabilities=["internet"]),
            _team_member(
                "argus",
                capabilities=["send_command", "slack:respond"],
            ),
            _team_member(
                "hermes", role="messenger",
                capabilities=["widget:respond", "widget:escalate"],
            ),
        ],
    }
    assert validate_team_plan(plan, reserved_names=set()) == []


def test_validate_team_plan_accepts_connector_prefix_capabilities():
    """The connector:<name> prefix is forward-compatible — a workspace
    can name an app-specific connector (e.g. connector:jyni_crm) even
    if the connector itself hasn't shipped as a Lightsei primitive yet.
    The validator accepts it; enforcement waits for the connector
    adapter to register."""
    plan = {
        "rationale": "test",
        "team": [
            _team_member(
                "vega",
                capabilities=["connector:gmail", "connector:jyni_crm"],
            ),
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    assert validate_team_plan(plan, reserved_names=set()) == []


def test_validate_team_plan_rejects_unknown_capability():
    """An unknown capability surfaces as a per-bot problem so the
    retry message can prompt the LLM to fix only the offending bot."""
    plan = {
        "rationale": "test",
        "team": [
            _team_member("vega", capabilities=["filesystem", "shell"]),
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    # Both bad entries flagged, both scoped to team[0].
    assert any("team[0].capabilities[0]" in p and "filesystem" in p for p in problems)
    assert any("team[0].capabilities[1]" in p and "shell" in p for p in problems)


def test_validate_team_plan_rejects_missing_capabilities():
    """The schema marks capabilities as required; if a bot somehow
    submits without it (malformed LLM output), the validator surfaces
    a clean 'capabilities must be a list' error so the retry message
    can fix it."""
    bad_member = {
        k: v for k, v in _team_member("vega").items() if k != "capabilities"
    }
    plan = {
        "rationale": "test",
        "team": [
            bad_member,
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    assert any("team[0].capabilities" in p for p in problems)


def test_validate_team_plan_rejects_duplicate_capability():
    """Duplicates within a single bot's list are flagged per-bot.
    Deduped at deploy via normalize_capability_list, but the planner
    shouldn't be emitting dupes in the first place."""
    plan = {
        "rationale": "test",
        "team": [
            _team_member(
                "vega",
                capabilities=["internet", "internet"],
            ),
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    problems = validate_team_plan(plan, reserved_names=set())
    assert any(
        "team[0].capabilities[1]" in p and "duplicate" in p for p in problems
    )


def test_submit_team_schema_requires_capabilities():
    """Tool schema explicitly lists `capabilities` in the per-bot
    required array; this is what gets the LLM to emit it at all.
    Locks in 24.1 so a future refactor doesn't silently drop it."""
    member_required = SUBMIT_TEAM_TOOL["input_schema"]["properties"]["team"][
        "items"
    ]["required"]
    assert "capabilities" in member_required


def test_validate_team_plan_accepts_realistic_jyni_shaped_plan():
    """Phase 24.4: lock the contract that a full JYNI-flavored plan
    round-trips clean through validate_team. Mirrors the 5-bot team
    the planner actually emitted for JYNI on 2026-05-24 (graded A+
    on quality in the Phase 23.10 test):

    - polaris (orchestrator, internal): daily tick coordinator, fans
      out to argus + vela via send_command.
    - argus (specialist, pii): org-isolation auditor + secret scanner;
      touches API routes that handle customer data. dispatches to
      hermes via send_command.
    - vela (specialist, internal): deploy + cron health verifier;
      system metadata only.
    - vega (specialist, sensitive): structural PR reviewer; sees
      diffs that may include sensitive (non-PII) code paths.
    - hermes (messenger, internal): outbound Slack notifier; strict
      leaf, slack:respond only.

    Also exercises the JYNI-specific freeform-constraint case: a
    `connector:jyni_crm` capability on a bot whose constraint came
    from the operator's freeform input, not the README.
    """
    plan = {
        "rationale": (
            "Five-bot team for the JYNI CRM x Scraper monorepo. "
            "Polaris orchestrates daily; argus + vela + vega each "
            "specialize; hermes posts findings to Slack."
        ),
        "team": [
            _team_member(
                "polaris", role="orchestrator",
                sensitivity_hint="internal",
                command_kinds=["polaris.tick"],
                dispatches_to=["argus", "vela"],
                capabilities=["send_command"],
            ),
            _team_member(
                "argus", role="specialist",
                sensitivity_hint="pii",
                command_kinds=["argus.scan"],
                dispatches_to=["hermes"],
                # Operator-side connector:jyni_crm illustrates the
                # freeform-constraint case from 24.1's prompt
                # ("bot Z can only read the CRM"). Forward-compat —
                # the connector adapter itself ships in 24B.
                capabilities=["send_command", "connector:jyni_crm"],
            ),
            _team_member(
                "vela", role="specialist",
                sensitivity_hint="internal",
                command_kinds=["vela.verify"],
                dispatches_to=["hermes"],
                capabilities=["send_command"],
            ),
            _team_member(
                "vega", role="specialist",
                sensitivity_hint="sensitive",
                command_kinds=["vega.review"],
                dispatches_to=["hermes"],
                capabilities=["send_command"],
            ),
            _team_member(
                "hermes", role="messenger",
                sensitivity_hint="internal",
                command_kinds=["hermes.post"],
                dispatches_to=[],
                capabilities=["slack:respond"],
            ),
        ],
    }
    assert validate_team_plan(plan, reserved_names=set()) == []


# ---------- Endpoint tests with stubbed Anthropic ---------- #


class _FakeUsage:
    def __init__(self, in_t: int, out_t: int) -> None:
        self.input_tokens = in_t
        self.output_tokens = out_t


def _fake_response(*, plan: dict, model: str = "claude-opus-4-7") -> SimpleNamespace:
    tool_block = SimpleNamespace(
        type="tool_use",
        name="submit_team",
        input=plan,
    )
    return SimpleNamespace(
        content=[tool_block],
        stop_reason="tool_use",
        model=model,
        usage=_FakeUsage(800, 1500),
    )


class _FakeClient:
    def __init__(self, *args, **kwargs) -> None:
        self.messages = SimpleNamespace(create=lambda **kw: None)


def _set_anthropic_secret(client, api_key: str, value: str = "sk-ant-fake"):
    r = client.put(
        "/workspaces/me/secrets/ANTHROPIC_API_KEY",
        headers=auth_headers(api_key),
        json={"value": value},
    )
    assert r.status_code == 200, r.text


def _set_budget_and_spend(client, api_key: str, workspace_id: str) -> None:
    r = client.patch(
        "/workspaces/me",
        headers=auth_headers(api_key),
        json={"budget_usd_monthly": 0.01},
    )
    assert r.status_code == 200, r.text
    s = SessionLocal()
    try:
        s.add(
            Run(
                id="budget-spend-team-plan",
                workspace_id=workspace_id,
                agent_name="polaris",
                started_at=datetime.now(timezone.utc),
                ended_at=None,
                cost_usd=Decimal("0.01"),
            )
        )
        s.commit()
    finally:
        s.close()


def _good_plan() -> dict:
    return {
        "rationale": "Three-bot team for a small backend project.",
        "team": [
            _team_member("vega", command_kinds=["vega.review"]),
            _team_member(
                "argus",
                command_kinds=["argus.scan"],
                dispatches_to=["hermes"],
            ),
            _team_member(
                "hermes",
                role="messenger",
                command_kinds=["hermes.post"],
            ),
        ],
    }


def test_plan_returns_team_with_valid_names(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_response(plan=_good_plan())
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    body = kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/teams/plan",
        body={
            "freeform_description": (
                "A small backend service with a Postgres DB. We push "
                "to main, deploy to Railway, want PR review and a "
                "secret scanner."
            ),
        },
    )
    assert body["rationale"]
    names = [m["name"] for m in body["team"]]
    assert "vega" in names
    assert "argus" in names
    assert "hermes" in names
    assert body["model_used"] == "claude-opus-4-7"


def test_plan_400_when_no_inputs_given(client, alice):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)
    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={},
    )
    assert r.status_code == 400
    assert "at least one" in r.json()["detail"].lower()


def test_plan_400_when_anthropic_secret_missing(client, alice):
    api_key = alice["api_key"]["plaintext"]
    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={"readme_text": "anything"},
    )
    assert r.status_code == 400
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_plan_429_when_workspace_budget_already_spent(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    workspace_id = alice["workspace"]["id"]
    _set_anthropic_secret(client, api_key)
    _set_budget_and_spend(client, api_key, workspace_id)

    fake = _FakeClient()
    fake.messages.create = lambda **kw: pytest.fail("Anthropic should not be called")
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={"readme_text": "anything"},
    )
    assert r.status_code == 429
    assert "budget" in r.json()["detail"].lower()


def test_team_plan_job_rechecks_budget_before_anthropic_call(
    client, alice, monkeypatch,
):
    api_key = alice["api_key"]["plaintext"]
    workspace_id = alice["workspace"]["id"]
    _set_anthropic_secret(client, api_key)
    _set_budget_and_spend(client, api_key, workspace_id)

    fake = _FakeClient()
    fake.messages.create = lambda **kw: pytest.fail("Anthropic should not be called")
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    s = SessionLocal()
    try:
        with pytest.raises(HTTPException) as exc:
            run_team_plan_job(
                s,
                workspace_id,
                {"readme_text": "anything"},
            )
    finally:
        s.close()
    assert exc.value.status_code == 429
    assert "budget" in exc.value.detail.lower()


def test_plan_retries_on_invalid_first_attempt(client, alice, monkeypatch):
    """First response includes an off-dictionary name; second response
    is clean. Endpoint retries once with corrective feedback."""
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    bad_plan = {
        "rationale": "broken",
        "team": [
            _team_member("invented-star"),
            _team_member("argus"),
            _team_member("hermes", role="messenger"),
        ],
    }
    calls = {"n": 0}
    def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_response(plan=bad_plan)
        return _fake_response(plan=_good_plan())

    fake = _FakeClient()
    fake.messages.create = fake_create
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/teams/plan",
        body={"readme_text": "small backend project"},
    )
    assert calls["n"] == 2


def test_plan_translates_anthropic_overloaded_to_503(client, alice, monkeypatch):
    """Phase 12C.1 follow-up: Anthropic 529 (overloaded) surfaces from
    the SDK as an APIStatusError after the SDK's own retries exhaust.
    The endpoint translates that into a 503 with retry guidance
    instead of a confusing 502."""
    import anthropic

    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    # Construct a real APIStatusError requires a real httpx Response;
    # instead build a tiny subclass that satisfies our `getattr(exc,
    # "status_code", None)` translation path. The endpoint doesn't care
    # how the SDK created the exception, only that `status_code == 529`.
    class _OverloadedError(anthropic.APIError):
        def __init__(self):
            self.status_code = 529
            self.message = "Overloaded"

        def __str__(self) -> str:
            return "Overloaded"

    fake = _FakeClient()

    def boom(**kw):
        raise _OverloadedError()

    fake.messages.create = boom
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with pytest.raises(GenerationJobFailed) as exc:
        kick_and_wait_for_job(
            client,
            auth_headers(api_key),
            path="/workspaces/me/teams/plan",
            body={"readme_text": "anything"},
        )
    # Persisted error wraps the FastAPI HTTPException text; check for
    # the same "overloaded" + "try again" guidance the old 503 carried.
    assert "overloaded" in exc.value.error.lower()
    assert "try again" in exc.value.error.lower()


def test_plan_422_when_retry_also_invalid(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    bad_plan = {
        "rationale": "still broken",
        "team": [
            _team_member("invented-star-1"),
            _team_member("invented-star-2"),
            _team_member("invented-star-3"),
        ],
    }
    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_response(plan=bad_plan)
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with pytest.raises(GenerationJobFailed) as exc:
        kick_and_wait_for_job(
            client,
            auth_headers(api_key),
            path="/workspaces/me/teams/plan",
            body={"readme_text": "anything"},
        )
    assert "remaining issues" in exc.value.error.lower()


def test_plan_records_cost_on_lightsei_system(client, alice, monkeypatch):
    """Phase 12D follow-up parity: server-side Anthropic spend lands
    on the synthetic `lightsei.system` agent so /cost reflects it."""
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_response(plan=_good_plan())
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/teams/plan",
        body={"readme_text": "small backend"},
    )

    # _FakeUsage(800, 1500) on claude-opus-4-7 = (800 * $15 + 1500 * $75) / 1M
    expected = (800 * 15.00 + 1500 * 75.00) / 1_000_000

    cost = client.get(
        "/workspaces/me/cost", headers=auth_headers(api_key)
    ).json()
    by_agent = {a["agent_name"]: a for a in cost["by_agent"]}
    assert "lightsei.system" in by_agent
    assert abs(by_agent["lightsei.system"]["mtd_usd"] - expected) < 1e-6


def test_plan_workspace_isolation(client, alice, bob, monkeypatch):
    """Bob calling /teams/plan without his own ANTHROPIC_API_KEY 400s
    even if alice has hers set — secrets are workspace-scoped."""
    _set_anthropic_secret(client, alice["api_key"]["plaintext"])

    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(bob["api_key"]["plaintext"]),
        json={"readme_text": "anything"},
    )
    assert r.status_code == 400
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_plan_unauthenticated(client):
    r = client.post(
        "/workspaces/me/teams/plan",
        json={"readme_text": "anything"},
    )
    assert r.status_code == 401


def test_plan_filters_lightsei_system_from_existing_agents(client, alice, monkeypatch):
    """`lightsei.system` is an accounting bucket; the team-planner
    prompt must not see it as an existing agent (would confuse the
    model and waste reserved-name slots)."""
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    # Create a `lightsei.system` row by hitting any endpoint that ensures
    # it. The test_plan_records_cost_on_lightsei_system test above already
    # creates one as a side effect, so call /teams/plan once first to seed.
    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_response(plan=_good_plan())
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)
    kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/teams/plan",
        body={"readme_text": "seed call"},
    )

    # Now intercept the prompt the second call sees.
    captured: dict = {}
    def capture_create(**kwargs):
        captured["system"] = kwargs.get("system")
        return _fake_response(plan=_good_plan())
    fake.messages.create = capture_create

    kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/teams/plan",
        body={"readme_text": "real call"},
    )
    assert "lightsei.system" not in captured["system"]
