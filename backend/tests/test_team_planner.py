"""Phase 12C.1: project-analysis endpoint.

Two surfaces under test:

1. The pure `team_planner` module — submit_team schema, prompt
   building, plan validation. No HTTP, no Anthropic, no DB.
2. `POST /workspaces/me/teams/plan` with a stubbed Anthropic client.
   Mirrors `test_agent_generator.py`'s _FakeClient pattern so CI
   doesn't need a real API key.
"""
from types import SimpleNamespace

import pytest

from team_planner import (
    MAX_DISPATCHES_PER_BOT,
    MAX_TEAM_SIZE,
    MIN_TEAM_SIZE,
    SUBMIT_TEAM_TOOL,
    build_system_prompt,
    build_user_message,
    validate_team_plan,
)
from tests.conftest import auth_headers


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
        "name", "role", "summary", "command_kinds",
        "dispatches_to", "needs_workspace_secrets", "draft_description",
    }
    # role is enum-constrained
    assert set(member["properties"]["role"]["enum"]) == {
        "orchestrator", "specialist", "messenger",
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
) -> dict:
    return {
        "name": name,
        "role": role,
        "summary": f"{name} does the thing",
        "command_kinds": command_kinds if command_kinds is not None else [f"{name}.do"],
        "dispatches_to": dispatches_to if dispatches_to is not None else [],
        "needs_workspace_secrets": [],
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

    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={
            "freeform_description": (
                "A small backend service with a Postgres DB. We push "
                "to main, deploy to Railway, want PR review and a "
                "secret scanner."
            ),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
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

    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={"readme_text": "small backend project"},
    )
    assert r.status_code == 200, r.text
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

    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={"readme_text": "anything"},
    )
    assert r.status_code == 503, r.text
    detail = r.json()["detail"]
    assert "overloaded" in detail.lower()
    assert "try again" in detail.lower()


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

    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={"readme_text": "anything"},
    )
    assert r.status_code == 422
    assert "remaining issues" in r.json()["detail"].lower()


def test_plan_records_cost_on_lightsei_system(client, alice, monkeypatch):
    """Phase 12D follow-up parity: server-side Anthropic spend lands
    on the synthetic `lightsei.system` agent so /cost reflects it."""
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_response(plan=_good_plan())
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={"readme_text": "small backend"},
    )
    assert r.status_code == 200

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
    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={"readme_text": "seed call"},
    )
    assert r.status_code == 200

    # Now intercept the prompt the second call sees.
    captured: dict = {}
    def capture_create(**kwargs):
        captured["system"] = kwargs.get("system")
        return _fake_response(plan=_good_plan())
    fake.messages.create = capture_create

    r = client.post(
        "/workspaces/me/teams/plan",
        headers=auth_headers(api_key),
        json={"readme_text": "real call"},
    )
    assert r.status_code == 200
    assert "lightsei.system" not in captured["system"]
