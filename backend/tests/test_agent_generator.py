"""Phase 12B.1: backend agent-code generation.

Two surfaces under test:

1. The pure `agent_generator` module — star dictionary, prompt-building
   helpers, schema. No HTTP, no Anthropic, no DB.
2. The `POST /workspaces/me/agents/generate` endpoint with a stubbed
   Anthropic client. We patch `anthropic.Anthropic` to return canned
   tool_use responses so CI doesn't need a real API key.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from agent_generator import (
    STAR_DICTIONARY,
    SUBMIT_BOT_TOOL,
    build_system_prompt,
    build_user_message,
    is_valid_star_name,
    render_star_dictionary_for_prompt,
    run_agent_generation_job,
)
from db import SessionLocal
from models import Run
from tests.conftest import (
    GenerationJobFailed,
    auth_headers,
    kick_and_wait_for_job,
)


# ---------- Pure-module unit tests ---------- #


def test_star_dictionary_has_seeded_agents():
    names = {n for n, _ in STAR_DICTIONARY}
    # The bots we ship out of the box must be in the dictionary so the
    # generator can recognize them as already-taken in their workspaces.
    assert {"polaris", "atlas", "hermes"}.issubset(names)


def test_is_valid_star_name_lowercase_and_strip():
    assert is_valid_star_name("polaris")
    assert is_valid_star_name("Polaris")
    assert is_valid_star_name("  ATLAS  ")
    assert not is_valid_star_name("nonstar")
    assert not is_valid_star_name("")
    assert not is_valid_star_name(None)  # type: ignore[arg-type]


def test_render_dictionary_filters_reserved_names():
    full = render_star_dictionary_for_prompt(reserved=set())
    filtered = render_star_dictionary_for_prompt(
        reserved={"polaris", "atlas"}
    )
    assert "polaris" in full
    assert "atlas" in full
    assert "polaris" not in filtered
    assert "atlas" not in filtered
    # Other names are still listed.
    assert "hermes" in filtered


def test_build_system_prompt_includes_agents_and_dictionary():
    prompt = build_system_prompt(
        existing_agents=[
            {
                "name": "polaris",
                "role": "orchestrator",
                "provider": None,
                "model": None,
                "command_kinds": ["polaris.evaluate_push"],
            },
        ],
        reserved_names={"polaris"},
    )
    # Existing agent rendered.
    assert "polaris" in prompt
    assert "polaris.evaluate_push" in prompt
    # Star dictionary excludes polaris (reserved).
    assert "| `polaris` |" not in prompt
    assert "| `hermes` |" in prompt
    # Always-on prompt scaffolding.
    assert "submit_bot" in prompt
    assert "lightsei.init" in prompt


def test_build_user_message_optional_hints():
    plain = build_user_message("post a haiku to slack")
    assert "post a haiku to slack" in plain
    assert "Coordinate" not in plain  # no targets passed

    full = build_user_message(
        "post a haiku to slack",
        target_agents=["hermes"],
        name_hint="vega",
    )
    assert "hermes" in full
    assert "vega" in full


def test_submit_bot_schema_required_fields():
    required = set(SUBMIT_BOT_TOOL["input_schema"]["required"])
    # Anything missing here breaks the dashboard's render contract.
    assert required == {"agent_name", "rationale", "bot_py", "requirements_txt"}


# ---------- Endpoint tests with stubbed Anthropic ---------- #


class _FakeUsage:
    def __init__(self, in_t: int, out_t: int) -> None:
        self.input_tokens = in_t
        self.output_tokens = out_t


# A minimally-valid bot the validation gate accepts: defines main(),
# imports only stdlib + lightsei (covered by the lightsei>=0.1.3 line).
_VALID_BOT_PY = '''import lightsei
import os
import time

lightsei.init(api_key=os.environ["LIGHTSEI_API_KEY"], agent_name="x")


def main():
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
'''


def _fake_response(*, agent_name: str, bot_py: str = _VALID_BOT_PY,
                   requirements_txt: str = "lightsei>=0.1.3\n",
                   rationale: str = "test bot") -> SimpleNamespace:
    """Build the `messages.create` return value the endpoint expects."""
    tool_block = SimpleNamespace(
        type="tool_use",
        name="submit_bot",
        input={
            "agent_name": agent_name,
            "rationale": rationale,
            "bot_py": bot_py,
            "requirements_txt": requirements_txt,
        },
    )
    return SimpleNamespace(
        content=[tool_block],
        stop_reason="tool_use",
        model="claude-opus-4-7",
        usage=_FakeUsage(123, 456),
    )


class _FakeClient:
    """Stand-in for `anthropic.Anthropic`. Configurable per-test by
    monkeypatching `messages.create` to return whatever response the
    test scenario wants."""

    def __init__(self, *args, **kwargs) -> None:
        # Each instance gets its own messages obj so tests can set
        # `.messages.create = lambda ...` without leakage.
        self.messages = SimpleNamespace(create=lambda **kw: None)


def _set_anthropic_secret(client, api_key: str, value: str = "sk-ant-fake"):
    """The endpoint reads ANTHROPIC_API_KEY from the workspace's secrets
    store. Set it once per test."""
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
                id="budget-spend-agent-generate",
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


def test_generate_returns_bot_with_dictionary_name(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    fake = _FakeClient()
    fake.messages.create = lambda **kwargs: _fake_response(
        agent_name="vega",
        rationale="Vega for code review.",
    )
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    body = kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/agents/generate",
        body={
            "description": "Build a code review bot that posts findings via hermes.",
        },
    )
    assert body["agent_name_suggestion"] == "vega"
    assert "lightsei.init" in body["bot_py"]
    assert "def main" in body["bot_py"]
    assert "lightsei>=" in body["requirements_txt"]
    assert body["rationale"] == "Vega for code review."
    assert body["model_used"] == "claude-opus-4-7"
    assert body["tokens_in"] == 123
    assert body["tokens_out"] == 456


def test_generate_400_when_anthropic_secret_missing(client, alice):
    api_key = alice["api_key"]["plaintext"]
    r = client.post(
        "/workspaces/me/agents/generate",
        headers=auth_headers(api_key),
        json={"description": "Build a hello-world bot."},
    )
    assert r.status_code == 400
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_generate_429_when_workspace_budget_already_spent(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    workspace_id = alice["workspace"]["id"]
    _set_anthropic_secret(client, api_key)
    _set_budget_and_spend(client, api_key, workspace_id)

    fake = _FakeClient()
    fake.messages.create = lambda **kw: pytest.fail("Anthropic should not be called")
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    r = client.post(
        "/workspaces/me/agents/generate",
        headers=auth_headers(api_key),
        json={"description": "Build a hello-world bot."},
    )
    assert r.status_code == 429
    assert "budget" in r.json()["detail"].lower()


def test_generate_job_rechecks_budget_before_anthropic_call(client, alice, monkeypatch):
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
            run_agent_generation_job(
                s,
                workspace_id,
                {"description": "Build a hello-world bot."},
            )
    finally:
        s.close()
    assert exc.value.status_code == 429
    assert "budget" in exc.value.detail.lower()


def test_generate_retries_when_name_off_dictionary(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    # First call returns an off-dictionary name; second returns a valid one.
    calls = {"n": 0}

    def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_response(agent_name="invented-name")
        return _fake_response(agent_name="vega")

    fake = _FakeClient()
    fake.messages.create = fake_create
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    body = kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/agents/generate",
        body={"description": "Build a code review bot."},
    )
    assert body["agent_name_suggestion"] == "vega"
    assert calls["n"] == 2  # one retry happened


def test_generate_422_when_retry_also_off_dictionary(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_response(agent_name="invented-name")
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with pytest.raises(GenerationJobFailed) as exc:
        kick_and_wait_for_job(
            client,
            auth_headers(api_key),
            path="/workspaces/me/agents/generate",
            body={"description": "Build a hello-world bot."},
        )
    assert "valid star name" in exc.value.error.lower()


def test_generate_retries_when_name_already_taken(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    # Make `polaris` an existing agent in alice's workspace by patching
    # one (PATCH endpoint auto-creates the row).
    r = client.patch(
        "/agents/polaris",
        headers=auth_headers(api_key),
        json={"system_prompt": "you are polaris"},
    )
    assert r.status_code == 200

    calls = {"n": 0}

    def fake_create(**kwargs):
        calls["n"] += 1
        # First attempt collides with the in-use polaris; retry picks vega.
        if calls["n"] == 1:
            return _fake_response(agent_name="polaris")
        return _fake_response(agent_name="vega")

    fake = _FakeClient()
    fake.messages.create = fake_create
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    body = kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/agents/generate",
        body={"description": "Build a planner."},
    )
    assert body["agent_name_suggestion"] == "vega"
    assert calls["n"] == 2


def test_generate_workspace_isolation(client, alice, bob, monkeypatch):
    """Bob calling generate without his own ANTHROPIC_API_KEY 400s even
    if alice has hers set — secrets are workspace-scoped."""
    _set_anthropic_secret(client, alice["api_key"]["plaintext"])
    r = client.post(
        "/workspaces/me/agents/generate",
        headers=auth_headers(bob["api_key"]["plaintext"]),
        json={"description": "Build a hello-world bot."},
    )
    assert r.status_code == 400
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_generate_short_description_validates(client, alice):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)
    r = client.post(
        "/workspaces/me/agents/generate",
        headers=auth_headers(api_key),
        json={"description": "x"},  # below min_length=8
    )
    assert r.status_code == 422  # Pydantic validation, not our handler


def test_generate_unauthenticated(client):
    r = client.post(
        "/workspaces/me/agents/generate",
        json={"description": "Build a hello-world bot."},
    )
    assert r.status_code == 401


# ---------- Phase 12B.4: validation gate ---------- #


def test_validate_clean_bot_returns_no_problems():
    from agent_generator import validate_generated_bot
    assert validate_generated_bot(_VALID_BOT_PY, "lightsei>=0.1.3\n") == []


def test_validate_catches_syntax_error():
    from agent_generator import validate_generated_bot
    bad = "def main(:\n    pass\n"
    problems = validate_generated_bot(bad, "lightsei>=0.1.3\n")
    assert any("SyntaxError" in p for p in problems)


def test_validate_catches_missing_main():
    from agent_generator import validate_generated_bot
    no_main = "import lightsei\nlightsei.init(api_key='x', agent_name='y')\n"
    problems = validate_generated_bot(no_main, "lightsei>=0.1.3\n")
    assert any("main" in p for p in problems)


def test_validate_catches_missing_lightsei_in_requirements():
    from agent_generator import validate_generated_bot
    problems = validate_generated_bot(_VALID_BOT_PY, "")
    assert any("lightsei>=0.1.3" in p for p in problems)


def test_validate_catches_undeclared_import():
    from agent_generator import validate_generated_bot
    bot_py = _VALID_BOT_PY.replace(
        "import time", "import time\nimport requests"
    )
    problems = validate_generated_bot(bot_py, "lightsei>=0.1.3\n")
    assert any("requests" in p for p in problems)


def test_validate_recognizes_dist_name_overrides():
    from agent_generator import validate_generated_bot
    # `import yaml` should be satisfied by `pyyaml` in requirements.
    bot_py = _VALID_BOT_PY.replace(
        "import time", "import time\nimport yaml"
    )
    problems = validate_generated_bot(
        bot_py, "lightsei>=0.1.3\npyyaml>=6.0\n"
    )
    assert problems == []


# ---------- Phase 21 follow-up (#65): psycopg2 + multi-override ---------- #


def test_validate_psycopg2_accepts_binary_variant():
    """The Phase 16 Coral demo's atlas bot imported psycopg2 with
    psycopg2-binary in requirements. The pre-21 validator wrongly
    flagged it as missing; the fix is to accept either dist name."""
    from agent_generator import validate_generated_bot
    bot_py = _VALID_BOT_PY.replace(
        "import time", "import time\nimport psycopg2"
    )
    # Operator picks the binary wheel — safer install on Railway.
    problems = validate_generated_bot(
        bot_py, "lightsei>=0.1.3\npsycopg2-binary>=2.9\n",
    )
    assert problems == []


def test_validate_psycopg2_also_accepts_source_dist():
    """Same import + source distribution `psycopg2` (not -binary) in
    requirements also passes. The dist name matches the literal
    module name; validator now accepts either."""
    from agent_generator import validate_generated_bot
    bot_py = _VALID_BOT_PY.replace(
        "import time", "import time\nimport psycopg2"
    )
    problems = validate_generated_bot(
        bot_py, "lightsei>=0.1.3\npsycopg2>=2.9\n",
    )
    assert problems == []


def test_validate_psycopg2_flags_when_neither_in_requirements():
    """If neither variant is declared, the validator still catches
    it. The error message points at the canonical (binary)
    variant so the LLM retry path picks the safer one."""
    from agent_generator import validate_generated_bot
    bot_py = _VALID_BOT_PY.replace(
        "import time", "import time\nimport psycopg2"
    )
    problems = validate_generated_bot(bot_py, "lightsei>=0.1.3\n")
    assert len(problems) == 1
    assert "psycopg2" in problems[0]
    assert "psycopg2-binary" in problems[0]


def test_validate_dist_name_overrides_extended_set():
    """Spot-check a few of the new overrides added in 21-follow-up:
    each should accept a literal-name OR an override-name dist."""
    from agent_generator import validate_generated_bot
    cases = [
        ("import sklearn", "scikit-learn>=1.3"),
        ("import dateutil", "python-dateutil>=2.8"),
        ("import dotenv", "python-dotenv>=1.0"),
        ("import MySQLdb", "mysqlclient>=2.2"),
    ]
    for import_line, reqs_line in cases:
        bot_py = _VALID_BOT_PY.replace(
            "import time", f"import time\n{import_line}"
        )
        reqs = f"lightsei>=0.1.3\n{reqs_line}\n"
        problems = validate_generated_bot(bot_py, reqs)
        assert problems == [], (
            f"expected no problems for {import_line!r} + {reqs_line!r}, "
            f"got {problems}"
        )


def test_generate_retries_when_validation_fails(client, alice, monkeypatch):
    """Validation gate: if the first generation has a SyntaxError or
    missing main(), the endpoint retries once with the problems
    appended as a corrective turn."""
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    bad_bot = "import lightsei\n# no main() defined\n"
    calls = {"n": 0}

    def fake_create(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_response(agent_name="vega", bot_py=bad_bot)
        return _fake_response(agent_name="vega")  # valid default

    fake = _FakeClient()
    fake.messages.create = fake_create
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    body = kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/agents/generate",
        body={"description": "Build a code review bot."},
    )
    assert calls["n"] == 2  # one retry happened
    assert "def main" in body["bot_py"]


def test_generate_422_when_validation_retry_also_fails(client, alice, monkeypatch):
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    bad_bot = "import lightsei\n# no main() defined\n"
    fake = _FakeClient()
    fake.messages.create = lambda **kw: _fake_response(
        agent_name="vega", bot_py=bad_bot
    )
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    with pytest.raises(GenerationJobFailed) as exc:
        kick_and_wait_for_job(
            client,
            auth_headers(api_key),
            path="/workspaces/me/agents/generate",
            body={"description": "Build a code review bot."},
        )
    assert "valid bot" in exc.value.error.lower()


# ---------- Phase 12B.3: iteration loop ---------- #


def test_iteration_message_includes_previous_and_tweak():
    from agent_generator import build_iteration_message
    msg = build_iteration_message(
        previous_bot_py="def main(): pass",
        previous_requirements_txt="lightsei>=0.1.3",
        tweak_request="poll every 30 minutes instead of 60",
    )
    assert "def main(): pass" in msg
    assert "lightsei>=0.1.3" in msg
    assert "30 minutes" in msg
    assert "submit_bot" in msg


def test_generate_iteration_includes_previous_in_messages(client, alice, monkeypatch):
    """When `tweak_request` + `previous_*` are set, the messages list
    sent to Claude includes both the original description AND the
    iteration turn referencing the prior bot."""
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    captured: dict[str, list] = {"messages": []}

    def fake_create(**kwargs):
        captured["messages"] = kwargs.get("messages", [])
        return _fake_response(agent_name="vega")

    fake = _FakeClient()
    fake.messages.create = fake_create
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/agents/generate",
        body={
            "description": "A code review bot",
            "tweak_request": "Make it post to slack via hermes",
            "previous_bot_py": "def main(): pass",
            "previous_requirements_txt": "lightsei>=0.1.3",
        },
    )
    msgs = captured["messages"]
    assert len(msgs) >= 2
    # First message: original description framing.
    assert "A code review bot" in msgs[0]["content"]
    # Second message: the iteration turn with prior bot + tweak.
    assert "def main(): pass" in msgs[1]["content"]
    assert "Make it post to slack via hermes" in msgs[1]["content"]


def test_generate_iteration_alone_without_previous_ignored(client, alice, monkeypatch):
    """If only `tweak_request` is set without the previous_* fields, we
    treat it as a fresh generation (no iteration turn appended)."""
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    captured: dict[str, list] = {"messages": []}

    def fake_create(**kwargs):
        captured["messages"] = kwargs.get("messages", [])
        return _fake_response(agent_name="vega")

    fake = _FakeClient()
    fake.messages.create = fake_create
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/agents/generate",
        body={
            "description": "A code review bot",
            "tweak_request": "Make it faster",
            # previous_* deliberately omitted.
        },
    )
    # Only the framing message; no iteration turn.
    assert len(captured["messages"]) == 1


def test_generate_records_cost_on_lightsei_system_run(client, alice, monkeypatch):
    """Phase 12D follow-up #1: server-side Anthropic calls bypass the
    SDK auto-patch, so the endpoint must commit a Run row itself or
    the spend never lands on /cost. The synthetic agent name is
    `lightsei.system` and is filtered out of /agents."""
    api_key = alice["api_key"]["plaintext"]
    _set_anthropic_secret(client, api_key)

    fake = _FakeClient()
    # _fake_response uses usage=(123, 456). claude-opus-4-7 is
    # ($15/M in, $75/M out).
    fake.messages.create = lambda **kw: _fake_response(agent_name="vega")
    monkeypatch.setattr("anthropic.Anthropic", lambda **kw: fake)

    kick_and_wait_for_job(
        client,
        auth_headers(api_key),
        path="/workspaces/me/agents/generate",
        body={"description": "Build a code review bot."},
    )

    expected = (123 * 15.00 + 456 * 75.00) / 1_000_000

    cost = client.get(
        "/workspaces/me/cost", headers=auth_headers(api_key)
    ).json()
    by_agent = {a["agent_name"]: a for a in cost["by_agent"]}
    assert "lightsei.system" in by_agent, (
        f"generation cost not attributed; saw {by_agent.keys()}"
    )
    assert abs(by_agent["lightsei.system"]["mtd_usd"] - expected) < 1e-6
    assert abs(cost["mtd_usd"] - expected) < 1e-6

    # And the synthetic agent must not appear on /agents — it's not a
    # user bot, just an accounting bucket.
    listed = client.get("/agents", headers=auth_headers(api_key)).json()
    names = [a["name"] for a in listed["agents"]]
    assert "lightsei.system" not in names
