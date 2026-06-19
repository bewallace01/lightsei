from tests.conftest import auth_headers


def test_enqueue_then_claim_then_complete(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])

    r = client.post(
        "/agents/demo/commands",
        json={"kind": "greet", "payload": {"who": "world"}},
        headers=h,
    )
    assert r.status_code == 200
    cmd = r.json()
    assert cmd["status"] == "pending"

    r = client.post("/agents/demo/commands/claim", headers=h)
    assert r.status_code == 200
    claimed = r.json()["command"]
    assert claimed["id"] == cmd["id"]
    assert claimed["status"] == "claimed"

    # Second claim returns nothing — the command is no longer pending.
    r = client.post("/agents/demo/commands/claim", headers=h)
    assert r.json()["command"] is None

    r = client.post(
        f"/commands/{cmd['id']}/complete",
        json={"result": {"greeting": "hi world"}},
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["result"] == {"greeting": "hi world"}


def test_complete_with_error_marks_failed(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    cmd = client.post(
        "/agents/demo/commands",
        json={"kind": "greet", "payload": {}},
        headers=h,
    ).json()
    client.post("/agents/demo/commands/claim", headers=h)

    r = client.post(
        f"/commands/{cmd['id']}/complete",
        json={"error": "boom"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["status"] == "failed"
    assert r.json()["error"] == "boom"


def test_complete_requires_claimed_command(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    cmd = client.post(
        "/agents/demo/commands",
        json={"kind": "greet", "payload": {}},
        headers=h,
    ).json()

    r = client.post(
        f"/commands/{cmd['id']}/complete",
        json={"result": {"greeting": "hi world"}},
        headers=h,
    )

    assert r.status_code == 400
    assert "claimed" in r.json()["detail"]


def test_cancel_pending_command(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    cmd = client.post(
        "/agents/demo/commands",
        json={"kind": "greet", "payload": {}},
        headers=h,
    ).json()

    r = client.delete(f"/commands/{cmd['id']}", headers=h)
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"

    # Cannot cancel an already-cancelled command.
    r = client.delete(f"/commands/{cmd['id']}", headers=h)
    assert r.status_code == 400


def test_command_cross_workspace_404(client, alice, bob):
    h_a = auth_headers(alice["api_key"]["plaintext"])
    h_b = auth_headers(bob["api_key"]["plaintext"])
    cmd = client.post(
        "/agents/demo/commands",
        json={"kind": "greet", "payload": {}},
        headers=h_a,
    ).json()

    # Bob can't complete or cancel alice's command.
    r = client.post(
        f"/commands/{cmd['id']}/complete", json={"result": {}}, headers=h_b,
    )
    assert r.status_code == 404
    r = client.delete(f"/commands/{cmd['id']}", headers=h_b)
    assert r.status_code == 404


def test_claim_only_pending_for_this_agent(client, alice):
    """The claim query filters by agent_name. A command for agent A must not
    be returned by a claim for agent B."""
    h = auth_headers(alice["api_key"]["plaintext"])
    client.post(
        "/agents/alpha/commands",
        json={"kind": "greet", "payload": {}},
        headers=h,
    )
    r = client.post("/agents/beta/commands/claim", headers=h)
    assert r.json()["command"] is None
