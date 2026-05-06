"""Bot-instance identity: per-process registration + heartbeat."""
import time
import uuid

from tests.conftest import auth_headers


def _heartbeat(client, headers, agent_name, instance_id, **extra):
    body = {"instance_id": instance_id}
    body.update(extra)
    return client.post(
        f"/agents/{agent_name}/instances/heartbeat",
        json=body,
        headers=headers,
    )


def test_first_heartbeat_registers_instance(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    iid = str(uuid.uuid4())

    r = _heartbeat(
        client, h, "demo", iid,
        hostname="laptop-01", pid=1234, sdk_version="0.0.1",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == iid
    assert body["hostname"] == "laptop-01"
    assert body["pid"] == 1234
    assert body["sdk_version"] == "0.0.1"
    assert body["status"] == "active"


def test_second_heartbeat_updates_timestamp(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    iid = str(uuid.uuid4())

    r1 = _heartbeat(client, h, "demo", iid, hostname="laptop-01")
    first_ts = r1.json()["last_heartbeat_at"]

    time.sleep(0.05)  # ensure measurable delta
    r2 = _heartbeat(client, h, "demo", iid)
    second_ts = r2.json()["last_heartbeat_at"]

    assert second_ts > first_ts


def test_list_instances_returns_active_status(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    iid = str(uuid.uuid4())
    _heartbeat(client, h, "demo", iid, hostname="laptop-01", pid=42)

    r = client.get("/agents/demo/instances", headers=h)
    assert r.status_code == 200
    instances = r.json()["instances"]
    assert len(instances) == 1
    assert instances[0]["id"] == iid
    assert instances[0]["status"] == "active"
    assert instances[0]["hostname"] == "laptop-01"


def test_instance_id_collision_across_workspaces_409(client, alice, bob):
    """Same instance_id submitted by two workspaces must not silently merge.
    Mirrors the run-id-collision behavior on /events."""
    h_a = auth_headers(alice["api_key"]["plaintext"])
    h_b = auth_headers(bob["api_key"]["plaintext"])
    iid = str(uuid.uuid4())

    r = _heartbeat(client, h_a, "demo", iid, hostname="alice-host")
    assert r.status_code == 200

    r = _heartbeat(client, h_b, "demo", iid, hostname="bob-host")
    assert r.status_code == 409


def test_instances_isolated_per_workspace(client, alice, bob):
    h_a = auth_headers(alice["api_key"]["plaintext"])
    h_b = auth_headers(bob["api_key"]["plaintext"])
    _heartbeat(client, h_a, "demo", str(uuid.uuid4()), hostname="alice-host")
    _heartbeat(client, h_b, "demo", str(uuid.uuid4()), hostname="bob-host")

    r = client.get("/agents/demo/instances", headers=h_a)
    hosts_a = [i["hostname"] for i in r.json()["instances"]]
    assert hosts_a == ["alice-host"]

    r = client.get("/agents/demo/instances", headers=h_b)
    hosts_b = [i["hostname"] for i in r.json()["instances"]]
    assert hosts_b == ["bob-host"]


def test_instances_filtered_by_agent_name(client, alice):
    """Two agents on the same workspace each have their own instance list."""
    h = auth_headers(alice["api_key"]["plaintext"])
    _heartbeat(client, h, "alpha", str(uuid.uuid4()), hostname="a-host")
    _heartbeat(client, h, "beta", str(uuid.uuid4()), hostname="b-host")

    r = client.get("/agents/alpha/instances", headers=h)
    assert [i["hostname"] for i in r.json()["instances"]] == ["a-host"]

    r = client.get("/agents/beta/instances", headers=h)
    assert [i["hostname"] for i in r.json()["instances"]] == ["b-host"]


def test_max_instances_per_hostname_refuses_new_registration(
    client, alice, monkeypatch
):
    """Backend caps concurrently-active instances of the same agent on
    the same hostname. The (cap+1)-th new registration is refused with
    409 so the runaway-process pattern fails loudly."""
    import main as main_mod

    monkeypatch.setattr(main_mod, "MAX_INSTANCES_PER_HOSTNAME", 2)

    h = auth_headers(alice["api_key"]["plaintext"])

    # Two new instances on the same hostname succeed.
    for _ in range(2):
        r = _heartbeat(
            client, h, "polaris", str(uuid.uuid4()),
            hostname="laptop-01", pid=1234, sdk_version="0.0.1",
        )
        assert r.status_code == 200, r.text

    # Third is refused with 409.
    iid = str(uuid.uuid4())
    r = _heartbeat(
        client, h, "polaris", iid,
        hostname="laptop-01", pid=4321, sdk_version="0.0.1",
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "polaris" in detail and "laptop-01" in detail

    # A different hostname is unaffected.
    r = _heartbeat(
        client, h, "polaris", str(uuid.uuid4()),
        hostname="laptop-02", pid=9999, sdk_version="0.0.1",
    )
    assert r.status_code == 200, r.text

    # And an existing instance on the capped host can still refresh its
    # own heartbeat — the cap only bites on first-registration.
    r = client.get("/agents/polaris/instances", headers=h)
    body = r.json()
    refreshing_id = next(
        i["id"] for i in body["instances"] if i["hostname"] == "laptop-01"
    )
    r = _heartbeat(
        client, h, "polaris", refreshing_id,
        hostname="laptop-01", pid=1234, sdk_version="0.0.1",
    )
    assert r.status_code == 200, r.text


def test_stale_instances_dont_count_toward_cap(client, alice, monkeypatch):
    """Once a heartbeat ages past INSTANCE_ACTIVE_WINDOW it's stale,
    so the slot frees up for a new process. Crashed-but-not-cleaned-up
    rows shouldn't keep blocking new bots forever."""
    import main as main_mod
    from datetime import timedelta

    monkeypatch.setattr(main_mod, "MAX_INSTANCES_PER_HOSTNAME", 1)
    monkeypatch.setattr(
        main_mod, "INSTANCE_ACTIVE_WINDOW", timedelta(milliseconds=1),
    )

    h = auth_headers(alice["api_key"]["plaintext"])
    r = _heartbeat(
        client, h, "polaris", str(uuid.uuid4()),
        hostname="laptop-01", pid=1, sdk_version="0.0.1",
    )
    assert r.status_code == 200
    time.sleep(0.05)  # let the first heartbeat go stale

    r = _heartbeat(
        client, h, "polaris", str(uuid.uuid4()),
        hostname="laptop-01", pid=2, sdk_version="0.0.1",
    )
    assert r.status_code == 200, r.text


def test_stale_instance_marked_stale(client, alice, monkeypatch):
    """If last_heartbeat_at is older than the active window, status is stale."""
    import main as main_mod
    from datetime import timedelta

    h = auth_headers(alice["api_key"]["plaintext"])
    iid = str(uuid.uuid4())
    _heartbeat(client, h, "demo", iid)

    # Tighten the active window so the test isn't slow.
    monkeypatch.setattr(
        main_mod, "INSTANCE_ACTIVE_WINDOW", timedelta(milliseconds=1),
    )
    time.sleep(0.05)

    r = client.get("/agents/demo/instances", headers=h)
    assert r.json()["instances"][0]["status"] == "stale"
