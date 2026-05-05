from tests.conftest import auth_headers


def test_patch_cap_only_does_not_clear_prompt(client, alice):
    """Regression: the partial-update path must respect model_fields_set so that
    PATCH-ing only one field never wipes the other."""
    h = auth_headers(alice["session_token"])

    # Set a system prompt first.
    r = client.patch(
        "/agents/demo",
        json={"system_prompt": "you are a helpful agent"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["system_prompt"] == "you are a helpful agent"

    # Now set a cap without sending system_prompt at all.
    r = client.patch(
        "/agents/demo",
        json={"daily_cost_cap_usd": 0.50},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["daily_cost_cap_usd"] == 0.50
    assert body["system_prompt"] == "you are a helpful agent"  # preserved


def test_patch_cap_null_clears(client, alice):
    h = auth_headers(alice["session_token"])
    client.patch("/agents/demo", json={"daily_cost_cap_usd": 1.00}, headers=h)
    r = client.patch("/agents/demo", json={"daily_cost_cap_usd": None}, headers=h)
    assert r.status_code == 200
    assert r.json()["daily_cost_cap_usd"] is None


def test_patch_system_prompt_whitespace_clears(client, alice):
    h = auth_headers(alice["session_token"])
    client.patch("/agents/demo", json={"system_prompt": "be nice"}, headers=h)
    r = client.patch("/agents/demo", json={"system_prompt": "   "}, headers=h)
    assert r.status_code == 200
    assert r.json()["system_prompt"] is None


def test_get_agent_404_when_unknown(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/agents/never-seen", headers=h)
    assert r.status_code == 404


# ---------- Phase 12.1: provider + model on Agent ---------- #


def test_patch_sets_provider_and_model(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/agents/atlas",
        json={"provider": "anthropic", "model": "claude-haiku-4-5"},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-haiku-4-5"

    # Round-trip via GET.
    r = client.get("/agents/atlas", headers=h)
    assert r.json()["provider"] == "anthropic"
    assert r.json()["model"] == "claude-haiku-4-5"


def test_patch_provider_unknown_value_returns_422(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/agents/atlas",
        json={"provider": "antropic"},  # typo
        headers=h,
    )
    assert r.status_code == 422
    assert "unknown provider" in str(r.json()["detail"]).lower()


def test_patch_provider_normalizes_case(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/agents/atlas",
        json={"provider": "Anthropic"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["provider"] == "anthropic"


def test_patch_provider_null_clears(client, alice):
    h = auth_headers(alice["session_token"])
    client.patch(
        "/agents/atlas",
        json={"provider": "openai", "model": "gpt-5"},
        headers=h,
    )
    r = client.patch(
        "/agents/atlas",
        json={"provider": None, "model": None},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["provider"] is None
    assert r.json()["model"] is None


def test_patch_provider_only_does_not_clear_model(client, alice):
    """Regression like test_patch_cap_only_does_not_clear_prompt: partial
    PATCH on provider+model fields must respect model_fields_set so a
    provider swap doesn't silently null the pinned model."""
    h = auth_headers(alice["session_token"])
    client.patch(
        "/agents/atlas",
        json={"provider": "anthropic", "model": "claude-haiku-4-5"},
        headers=h,
    )
    r = client.patch(
        "/agents/atlas",
        json={"provider": "google"},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "google"
    assert body["model"] == "claude-haiku-4-5"  # preserved


def test_list_agents_serializes_provider_and_model(client, alice):
    h = auth_headers(alice["session_token"])
    client.patch(
        "/agents/atlas",
        json={"provider": "google", "model": "gemini-1.5-flash"},
        headers=h,
    )
    r = client.get("/agents", headers=h)
    assert r.status_code == 200
    atlas = next(a for a in r.json()["agents"] if a["name"] == "atlas")
    assert atlas["provider"] == "google"
    assert atlas["model"] == "gemini-1.5-flash"


# ---------- Per-agent tick interval ---------- #


def test_patch_sets_tick_interval(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/agents/polaris",
        json={"tick_interval_s": 300},
        headers=h,
    )
    assert r.status_code == 200, r.text
    assert r.json()["tick_interval_s"] == 300
    # Round-trip via GET.
    assert client.get("/agents/polaris", headers=h).json()["tick_interval_s"] == 300


def test_patch_tick_interval_null_clears(client, alice):
    h = auth_headers(alice["session_token"])
    client.patch("/agents/polaris", json={"tick_interval_s": 600}, headers=h)
    r = client.patch(
        "/agents/polaris", json={"tick_interval_s": None}, headers=h,
    )
    assert r.status_code == 200
    assert r.json()["tick_interval_s"] is None


def test_patch_tick_interval_too_small_returns_422(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/agents/polaris", json={"tick_interval_s": 5}, headers=h,
    )
    assert r.status_code == 422
    assert "60" in r.json()["detail"]  # min bound


def test_patch_tick_interval_too_large_returns_422(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.patch(
        "/agents/polaris", json={"tick_interval_s": 999999}, headers=h,
    )
    assert r.status_code == 422
    assert "86400" in r.json()["detail"]  # max bound


def test_delete_agent_removes_row(client, alice):
    h = auth_headers(alice["session_token"])
    # Create one via PATCH (auto-creates).
    client.patch(
        "/agents/old-test-bot",
        json={"description": "leftover"},
        headers=h,
    )
    assert client.get("/agents/old-test-bot", headers=h).status_code == 200
    r = client.delete("/agents/old-test-bot", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert client.get("/agents/old-test-bot", headers=h).status_code == 404


def test_delete_agent_404_when_unknown(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.delete("/agents/never-existed", headers=h)
    assert r.status_code == 404


def test_delete_agent_workspace_isolated(client, alice, bob):
    """Bob can't delete alice's agent by guessing the name."""
    h_a = auth_headers(alice["session_token"])
    h_b = auth_headers(bob["session_token"])
    client.patch("/agents/alice-bot", json={"description": "hers"}, headers=h_a)
    r = client.delete("/agents/alice-bot", headers=h_b)
    assert r.status_code == 404
    # Still exists in alice's workspace.
    assert client.get("/agents/alice-bot", headers=h_a).status_code == 200


def test_patch_tick_interval_does_not_clear_other_fields(client, alice):
    h = auth_headers(alice["session_token"])
    client.patch(
        "/agents/polaris",
        json={"system_prompt": "be concise", "provider": "anthropic"},
        headers=h,
    )
    r = client.patch(
        "/agents/polaris", json={"tick_interval_s": 300}, headers=h,
    )
    body = r.json()
    assert body["tick_interval_s"] == 300
    assert body["system_prompt"] == "be concise"
    assert body["provider"] == "anthropic"
