"""Phase 9.1: notification channel CRUD + masking + isolation.

Tests live here for the API surface; Phase 9.2 will add dispatcher
tests in a sibling file once the formatters land.
"""
from sqlalchemy import select

from models import NotificationChannel, NotificationDelivery
from tests.conftest import auth_headers


SLACK_URL = "https://hooks.slack.com/services/T01ABCDEF/B02ABCDEF/abcdef1234567890"
DISCORD_URL = "https://discord.com/api/webhooks/123456789/SECRETTOKENVALUE12345"
TEAMS_URL = (
    "https://prod-12.westus.logic.azure.com:443/workflows/abc123/triggers/"
    "manual/paths/invoke?api-version=2016-06-01&sig=SECRETSIGNATURE"
)
WEBHOOK_URL = "https://example.com/hooks/abc-secret-token-xyz"


def _create(client, headers, **overrides):
    body = {
        "name": "primary",
        "type": "slack",
        "target_url": SLACK_URL,
        "triggers": ["polaris.plan", "validation.fail"],
    }
    body.update(overrides)
    r = client.post("/workspaces/me/notifications", json=body, headers=headers)
    return r


# ---------- happy paths per channel type ---------- #


def test_create_slack_channel(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _create(client, h, name="team-slack", type="slack", target_url=SLACK_URL)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["type"] == "slack"
    assert body["name"] == "team-slack"
    assert body["is_active"] is True
    assert body["triggers"] == ["polaris.plan", "validation.fail"]
    # URL is masked: scheme + host preserved, secret path truncated
    assert body["target_url_masked"].startswith("https://hooks.slack.com")
    assert "abcdef1234567890" not in body["target_url_masked"]


def test_create_discord_channel(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _create(client, h, name="team-discord", type="discord", target_url=DISCORD_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "discord"
    assert "SECRETTOKENVALUE" not in body["target_url_masked"]


def test_create_teams_channel(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _create(client, h, name="team-teams", type="teams", target_url=TEAMS_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "teams"
    assert "SECRETSIGNATURE" not in body["target_url_masked"]


def test_create_mattermost_channel(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _create(
        client, h, name="team-mm", type="mattermost",
        target_url="https://mattermost.example.com/hooks/secrettokenpath123",
    )
    assert r.status_code == 200
    assert r.json()["type"] == "mattermost"


def test_create_webhook_channel_with_secret(client, alice):
    """Generic webhook with HMAC secret_token. The token is opaque to
    the API — we just store it. has_secret_token=True surfaces presence
    without echoing the value."""
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _create(
        client, h, name="team-webhook", type="webhook",
        target_url=WEBHOOK_URL, secret_token="shared-secret-value-1234",
    )
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "webhook"
    assert body["has_secret_token"] is True
    # Make sure the secret never appears in the response anywhere
    assert "shared-secret-value-1234" not in str(body)


# ---------- input validation ---------- #


def test_create_rejects_unknown_type(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _create(client, h, type="telegram")  # not in v1
    assert r.status_code == 400
    assert "type must be one of" in r.json()["detail"]


def test_create_rejects_unknown_trigger(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _create(client, h, triggers=["polaris.plan", "made_up_trigger"])
    assert r.status_code == 400
    assert "made_up_trigger" in r.json()["detail"]


def test_create_rejects_malformed_name(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    r = _create(client, h, name="-leading-hyphen")
    assert r.status_code == 400


def test_create_409_on_duplicate_name(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    assert _create(client, h, name="primary").status_code == 200
    r = _create(client, h, name="primary")
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]


# ---------- list / get / patch / delete ---------- #


def test_list_returns_workspace_channels_only(client, alice, bob):
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    _create(client, ha, name="alice-only")

    listed_a = client.get("/workspaces/me/notifications", headers=ha).json()
    listed_b = client.get("/workspaces/me/notifications", headers=hb).json()
    assert len(listed_a["channels"]) == 1
    assert listed_a["channels"][0]["name"] == "alice-only"
    assert listed_b["channels"] == []


def test_get_one_404_for_other_workspace(client, alice, bob):
    """Bob can't read alice's channel by guessing the id."""
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    cid = _create(client, ha, name="alice-channel").json()["id"]

    assert client.get(
        f"/workspaces/me/notifications/{cid}", headers=ha
    ).status_code == 200
    assert client.get(
        f"/workspaces/me/notifications/{cid}", headers=hb
    ).status_code == 404


def test_patch_updates_triggers_and_active(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    cid = _create(
        client, h, name="primary", triggers=["polaris.plan"]
    ).json()["id"]

    r = client.patch(
        f"/workspaces/me/notifications/{cid}",
        json={
            "triggers": ["polaris.plan", "validation.fail", "run_failed"],
            "is_active": False,
        },
        headers=h,
    )
    assert r.status_code == 200
    body = r.json()
    assert sorted(body["triggers"]) == ["polaris.plan", "run_failed", "validation.fail"]
    assert body["is_active"] is False


def test_patch_rename_409_on_conflict(client, alice):
    """Renaming a channel to a name another channel in the same
    workspace already has should 409."""
    h = auth_headers(alice["api_key"]["plaintext"])
    cid_a = _create(client, h, name="alpha").json()["id"]
    _create(client, h, name="beta")
    r = client.patch(
        f"/workspaces/me/notifications/{cid_a}",
        json={"name": "beta"},
        headers=h,
    )
    assert r.status_code == 409


def test_patch_clear_secret_token(client, alice):
    """PATCHing secret_token to null clears it; not setting the field
    leaves it alone."""
    h = auth_headers(alice["api_key"]["plaintext"])
    cid = _create(
        client, h, name="primary", type="webhook", target_url=WEBHOOK_URL,
        secret_token="initial-secret",
    ).json()["id"]

    # Patch with no secret_token: leave alone.
    client.patch(
        f"/workspaces/me/notifications/{cid}",
        json={"is_active": True},
        headers=h,
    )
    assert client.get(
        f"/workspaces/me/notifications/{cid}", headers=h
    ).json()["has_secret_token"] is True

    # Patch with secret_token=null: clear.
    client.patch(
        f"/workspaces/me/notifications/{cid}",
        json={"secret_token": None},
        headers=h,
    )
    assert client.get(
        f"/workspaces/me/notifications/{cid}", headers=h
    ).json()["has_secret_token"] is False


def test_delete_removes_channel(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    cid = _create(client, h, name="going-away").json()["id"]
    assert client.delete(
        f"/workspaces/me/notifications/{cid}", headers=h
    ).status_code == 200
    assert client.get(
        f"/workspaces/me/notifications/{cid}", headers=h
    ).status_code == 404


def test_delete_404_for_other_workspace(client, alice, bob):
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    cid = _create(client, ha, name="alice-only").json()["id"]
    assert client.delete(
        f"/workspaces/me/notifications/{cid}", headers=hb
    ).status_code == 404
    # Channel still exists for alice
    assert client.get(
        f"/workspaces/me/notifications/{cid}", headers=ha
    ).status_code == 200


# ---------- test-fire (Phase 9.2 real dispatch) ---------- #


def test_test_fire_records_real_dispatch_attempt(client, alice):
    """Phase 9.2: the test-fire endpoint now does a real HTTP-out via
    the per-platform formatter. The configured URL doesn't reach a
    real Slack workspace, so the delivery lands as `failed` with an
    http_error or transport_error response_summary — but the row IS
    written. The endpoint returns 200 either way; it doesn't surface
    webhook failures as 5xx."""
    from db import engine
    from sqlalchemy.orm import Session as ORMSession

    h = auth_headers(alice["api_key"]["plaintext"])
    cid = _create(client, h, name="test-target").json()["id"]

    r = client.post(
        f"/workspaces/me/notifications/{cid}/test", headers=h
    )
    assert r.status_code == 200
    body = r.json()
    assert body["delivery"]["trigger"] == "test"
    # The fixture URL is hooks.slack.com/services/T01ABCDEF/... — Slack
    # itself responds 4xx with `invalid_token` for fake credentials.
    # Either http_error (Slack served a 4xx) or transport_error
    # (network blocked entirely) is a valid Phase 9.2 outcome; what
    # matters is that the endpoint records the attempt cleanly.
    assert body["delivery"]["status"] in ("sent", "failed")
    summary = body["delivery"]["response_summary"]
    # response_summary always has *something* — http_status on success/
    # 4xx, error on transport-level failures.
    assert summary, "response_summary should never be empty"

    with ORMSession(engine) as s:
        rows = s.execute(
            select(NotificationDelivery).where(
                NotificationDelivery.channel_id == cid
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status in ("sent", "failed")


def test_deliveries_endpoint_lists_recent_attempts(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    cid = _create(client, h, name="audit-target").json()["id"]
    # Three test-fires
    for _ in range(3):
        client.post(f"/workspaces/me/notifications/{cid}/test", headers=h)

    r = client.get(
        f"/workspaces/me/notifications/{cid}/deliveries", headers=h
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["deliveries"]) == 3
    # Newest first
    timestamps = [d["sent_at"] for d in body["deliveries"]]
    assert timestamps == sorted(timestamps, reverse=True)


def test_deliveries_404_for_other_workspace(client, alice, bob):
    ha = auth_headers(alice["api_key"]["plaintext"])
    hb = auth_headers(bob["api_key"]["plaintext"])
    cid = _create(client, ha, name="alice-only").json()["id"]
    assert client.get(
        f"/workspaces/me/notifications/{cid}/deliveries", headers=hb
    ).status_code == 404


def test_deliveries_validates_limit(client, alice):
    h = auth_headers(alice["api_key"]["plaintext"])
    cid = _create(client, h, name="primary").json()["id"]
    assert client.get(
        f"/workspaces/me/notifications/{cid}/deliveries?limit=0", headers=h
    ).status_code == 400
    assert client.get(
        f"/workspaces/me/notifications/{cid}/deliveries?limit=201", headers=h
    ).status_code == 400


# ---------- masking helper unit test ---------- #


def test_mask_url_helper_directly():
    """Sanity-check the masking helper across all the URL shapes the
    channel types use. Lives here rather than in a separate file
    because it's tightly coupled to the endpoint behavior."""
    from main import _mask_url
    # Slack: long path, gets truncated
    masked = _mask_url(SLACK_URL)
    assert masked.startswith("https://hooks.slack.com")
    assert "..." in masked
    assert "abcdef1234567890" not in masked
    # Generic webhook
    masked = _mask_url("https://example.com/hooks/very-secret-path")
    assert masked.startswith("https://example.com")
    # Garbage in -> *** out, not crash
    assert _mask_url("not-a-url") == "***"
    assert _mask_url("") == "***"


# ---------- auth ---------- #


def test_unauthorized():
    """Sanity check that the endpoints require auth."""
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as c:
        assert c.get("/workspaces/me/notifications").status_code == 401
        assert c.post(
            "/workspaces/me/notifications",
            json={"name": "x", "type": "slack", "target_url": SLACK_URL},
        ).status_code == 401
