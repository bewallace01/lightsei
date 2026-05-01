"""Phase 10.2: POST /webhooks/github — HMAC verification, event filtering,
path matching against registered agent paths.

The endpoint is public (GitHub posts here, no Lightsei API key). All
authentication is HMAC-SHA256 of the raw body using the webhook secret
revealed once during the 10.1 registration round-trip. Tests register
an integration via the 10.1 endpoints, capture the plaintext secret
from the PUT response, and use that secret to sign webhook payloads.

We don't mock the github_api module here — these tests don't reach
GitHub. They only exercise the receiver. PAT validation on registration
still hits the github_api mock (reused from test_github.py).
"""
import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx

import github_api
from tests.conftest import auth_headers


# ---------- shared helpers ---------- #


@contextmanager
def _mock_pat_ok():
    """Phase 10.1 PUT validates the PAT against GitHub. We don't care
    about that path for webhook tests, so always return 200."""
    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200,
            json={
                "full_name": "acme/widgets",
                "default_branch": "main",
                "private": False,
            },
        )
    )
    real_client = httpx.Client

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=transport, **kwargs)

    with patch.object(github_api.httpx, "Client", side_effect=factory):
        yield


def _register_integration(
    client,
    api_key: str,
    *,
    repo_owner: str = "acme",
    repo_name: str = "widgets",
    branch: str = "main",
) -> str:
    """Register a fresh integration and return the plaintext webhook
    secret revealed by PUT (needed to sign webhook payloads in tests)."""
    h = auth_headers(api_key)
    with _mock_pat_ok():
        r = client.put(
            "/workspaces/me/github",
            json={
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "branch": branch,
                "pat": "ghp_test_token_value_for_webhook_tests",
            },
            headers=h,
        )
    assert r.status_code == 200, r.text
    secret = r.json()["webhook_secret"]
    assert isinstance(secret, str) and len(secret) >= 30
    return secret


def _register_agent_path(client, api_key: str, agent_name: str, path: str) -> None:
    h = auth_headers(api_key)
    r = client.put(
        f"/workspaces/me/github/agents/{agent_name}",
        json={"path": path},
        headers=h,
    )
    assert r.status_code == 200, r.text


def _sign(secret: str, body: bytes) -> str:
    """GitHub's X-Hub-Signature-256 format: `sha256=<hex>`."""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _push_payload(
    *,
    owner: str = "acme",
    name: str = "widgets",
    ref: str = "refs/heads/main",
    after: str = "abc123def456",
    commits: list[dict] | None = None,
) -> dict:
    """Minimal GitHub `push` event payload with the fields our receiver
    actually reads. Real payloads have many more fields — we omit them
    so any future code that reaches into them surfaces in tests."""
    if commits is None:
        commits = [
            {
                "id": after,
                "added": [],
                "modified": [],
                "removed": [],
            }
        ]
    return {
        "ref": ref,
        "after": after,
        "repository": {"full_name": f"{owner}/{name}"},
        "head_commit": {"id": after},
        "commits": commits,
    }


def _post_webhook(
    client,
    payload: dict,
    *,
    secret: str | None,
    event: str = "push",
    sign: bool = True,
    bad_signature: bool = False,
):
    """POST to /webhooks/github. If `secret` is provided and `sign` is
    True, computes a real HMAC. `bad_signature=True` flips one byte to
    simulate a tampered signature."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event}
    if secret and sign:
        sig = _sign(secret, body)
        if bad_signature:
            # Flip one hex char so the digest no longer matches.
            sig = sig[:-1] + ("0" if sig[-1] != "0" else "1")
        headers["X-Hub-Signature-256"] = sig
    return client.post(
        "/webhooks/github",
        content=body,
        headers=headers,
    )


# ---------- HMAC verification ---------- #


def test_signed_push_event_is_accepted(client, alice):
    secret = _register_integration(client, alice["api_key"]["plaintext"])
    payload = _push_payload()
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok"
    assert body["event"] == "push"
    # No agent paths registered → no redeploys queued.
    assert body["queued_redeploys"] == []


def test_unsigned_request_is_rejected(client, alice):
    _register_integration(client, alice["api_key"]["plaintext"])
    payload = _push_payload()
    r = _post_webhook(client, payload, secret=None, sign=False)
    assert r.status_code == 401
    assert "signature" in r.json()["detail"].lower()


def test_signed_with_wrong_secret_is_rejected(client, alice):
    _register_integration(client, alice["api_key"]["plaintext"])
    payload = _push_payload()
    # Sign with a secret that isn't the one stored.
    r = _post_webhook(client, payload, secret="not-the-real-secret-bytes-1234567890")
    assert r.status_code == 401


def test_tampered_signature_is_rejected(client, alice):
    secret = _register_integration(client, alice["api_key"]["plaintext"])
    payload = _push_payload()
    r = _post_webhook(client, payload, secret=secret, bad_signature=True)
    assert r.status_code == 401


def test_tampered_body_after_signing_is_rejected(client, alice):
    """Sign one body, then send a different one. The receiver computes
    the digest over what arrived, not over what we claimed to sign."""
    secret = _register_integration(client, alice["api_key"]["plaintext"])
    body = json.dumps(_push_payload()).encode("utf-8")
    sig = _sign(secret, body)
    # Now send a DIFFERENT body with the original signature.
    tampered = json.dumps(_push_payload(after="ffff999")).encode("utf-8")
    r = client.post(
        "/webhooks/github",
        content=tampered,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": sig,
        },
    )
    assert r.status_code == 401


# ---------- repo lookup ---------- #


def test_unknown_repo_returns_404(client, alice):
    """Webhook for a repo no workspace has registered. We don't even
    have a secret to verify against, so we 404 before the HMAC check."""
    _register_integration(client, alice["api_key"]["plaintext"])
    payload = _push_payload(owner="someoneelse", name="somethingelse")
    # Signing with a guessed secret doesn't matter — repo lookup fails first.
    r = _post_webhook(client, payload, secret="anything")
    assert r.status_code == 404
    assert "someoneelse/somethingelse" in r.json()["detail"]


def test_malformed_body_returns_400(client, alice):
    _register_integration(client, alice["api_key"]["plaintext"])
    r = client.post(
        "/webhooks/github",
        content=b"not json at all",
        headers={"Content-Type": "application/json", "X-GitHub-Event": "push"},
    )
    assert r.status_code == 400


def test_missing_repository_field_returns_400(client, alice):
    _register_integration(client, alice["api_key"]["plaintext"])
    r = client.post(
        "/webhooks/github",
        content=json.dumps({"ref": "refs/heads/main"}).encode(),
        headers={"Content-Type": "application/json", "X-GitHub-Event": "push"},
    )
    assert r.status_code == 400


# ---------- event filtering ---------- #


def test_ping_event_is_acknowledged_with_no_redeploys(client, alice):
    """GitHub fires `ping` immediately after webhook creation to test
    the URL. We must accept it and explicitly do nothing."""
    secret = _register_integration(client, alice["api_key"]["plaintext"])
    # Ping payloads have a `zen` quote and a `repository` block but no
    # `commits` array. Our receiver should never look at commits for
    # non-push events.
    payload = {
        "zen": "Speak like a human.",
        "repository": {"full_name": "acme/widgets"},
    }
    r = _post_webhook(client, payload, secret=secret, event="ping")
    assert r.status_code == 200
    body = r.json()
    assert body["event"] == "ping"
    assert "queued_redeploys" not in body


def test_unhandled_event_type_is_acknowledged(client, alice):
    secret = _register_integration(client, alice["api_key"]["plaintext"])
    payload = _push_payload()
    r = _post_webhook(client, payload, secret=secret, event="pull_request")
    assert r.status_code == 200
    body = r.json()
    assert body["event"] == "pull_request"
    assert body["skipped"] == "event_type_not_handled"


def test_push_to_untracked_branch_is_acknowledged_with_no_redeploys(client, alice):
    """Push to `feature/foo` when only `main` is tracked — 200 no-op."""
    secret = _register_integration(client, alice["api_key"]["plaintext"])
    payload = _push_payload(ref="refs/heads/feature/foo")
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == "branch_not_tracked"
    assert body["tracked_branch"] == "main"


def test_inactive_integration_quietly_accepts(client, alice):
    """If the integration row has is_active=False (currently nothing
    flips it, but the schema supports it), the receiver should still
    200 — GitHub stops retrying — but skip everything else."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)

    # Manually flip is_active via the DB session. There's no endpoint
    # for this in 10.1 (deactivate-without-delete is a 10B feature), so
    # we reach into the SQLAlchemy session directly.
    from sqlalchemy import update
    from db import engine
    from models import GitHubIntegration

    with engine.begin() as conn:
        conn.execute(
            update(GitHubIntegration)
            .where(GitHubIntegration.repo_owner == "acme")
            .values(is_active=False)
        )

    payload = _push_payload()
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == "integration_inactive"


# ---------- path matching ---------- #


def test_push_with_no_registered_paths_queues_nothing(client, alice):
    """An integration is registered but no agent paths are. Every push
    accepts cleanly with an empty queued list."""
    secret = _register_integration(client, alice["api_key"]["plaintext"])
    payload = _push_payload(
        commits=[
            {
                "id": "abc",
                "added": ["src/foo.py"],
                "modified": ["README.md"],
                "removed": [],
            }
        ]
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    assert r.json()["queued_redeploys"] == []


def test_push_touching_registered_path_queues_one_redeploy(client, alice):
    """Map agent `polaris` to `polaris/`. A push that modifies
    `polaris/bot.py` should queue exactly one redeploy for `polaris`."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    payload = _push_payload(
        after="cafe1234",
        commits=[
            {
                "id": "cafe1234",
                "added": [],
                "modified": ["polaris/bot.py"],
                "removed": [],
            }
        ],
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    body = r.json()
    assert body["commit_sha"] == "cafe1234"
    assert body["queued_redeploys"] == [
        {"agent_name": "polaris", "commit_sha": "cafe1234"}
    ]


def test_push_only_outside_registered_paths_queues_nothing(client, alice):
    """Map polaris→polaris/. A push that only touches `docs/` should
    NOT queue a polaris redeploy."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    payload = _push_payload(
        commits=[
            {
                "id": "abc",
                "added": ["docs/intro.md"],
                "modified": ["README.md"],
                "removed": [],
            }
        ]
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    assert r.json()["queued_redeploys"] == []


def test_path_match_is_directory_aware_not_prefix_substring(client, alice):
    """`polaris/` should NOT match `polarisXYZ/foo.py` — the path is a
    directory boundary, not a string prefix."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    payload = _push_payload(
        commits=[{"id": "x", "modified": ["polarisXYZ/foo.py"], "added": [], "removed": []}]
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    assert r.json()["queued_redeploys"] == []


def test_multiple_registered_paths_only_matching_one_redeploys(client, alice):
    """Map polaris→polaris/, scout→bots/scout/. A push that touches
    only polaris/ files should queue ONE redeploy (polaris), not two."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")
    _register_agent_path(client, api_key, "scout", "bots/scout")

    payload = _push_payload(
        after="deadbeef",
        commits=[{"id": "deadbeef", "modified": ["polaris/policy.py"], "added": [], "removed": []}],
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    queued = r.json()["queued_redeploys"]
    assert len(queued) == 1
    assert queued[0]["agent_name"] == "polaris"


def test_push_touching_paths_for_two_agents_queues_two_redeploys(client, alice):
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")
    _register_agent_path(client, api_key, "scout", "bots/scout")

    payload = _push_payload(
        after="multi1",
        commits=[
            {
                "id": "multi1",
                "modified": ["polaris/policy.py", "bots/scout/main.py"],
                "added": [],
                "removed": [],
            }
        ],
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    agents = sorted(q["agent_name"] for q in r.json()["queued_redeploys"])
    assert agents == ["polaris", "scout"]


def test_push_match_via_added_or_removed_files(client, alice):
    """Path-touch detection looks at added + modified + removed, not
    just modified — a deleted file under the agent's path still counts
    as a redeploy trigger."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    payload_added = _push_payload(
        commits=[{"id": "a", "added": ["polaris/new.py"], "modified": [], "removed": []}]
    )
    r = _post_webhook(client, payload_added, secret=secret)
    assert r.json()["queued_redeploys"][0]["agent_name"] == "polaris"

    payload_removed = _push_payload(
        commits=[{"id": "b", "added": [], "modified": [], "removed": ["polaris/old.py"]}]
    )
    r = _post_webhook(client, payload_removed, secret=secret)
    assert r.json()["queued_redeploys"][0]["agent_name"] == "polaris"


def test_push_match_when_commits_span_multiple_entries(client, alice):
    """A single push can carry many commits; we must scan all of them,
    not just head_commit. (head_commit is just a cached pointer.)"""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    payload = _push_payload(
        after="head1",
        commits=[
            {"id": "earlier", "added": [], "modified": ["polaris/early.py"], "removed": []},
            {"id": "head1", "added": [], "modified": ["README.md"], "removed": []},
        ],
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    assert r.json()["queued_redeploys"][0]["agent_name"] == "polaris"


# ---------- workspace isolation ---------- #


def test_other_workspace_repo_does_not_leak_redeploys(client, alice, bob):
    """Alice and Bob each register a different repo. A webhook for
    Bob's repo must look up Bob's integration (and Bob's agent paths),
    never Alice's — even if Alice has paths registered."""
    alice_secret = _register_integration(
        client, alice["api_key"]["plaintext"], repo_owner="alice", repo_name="repo"
    )
    _register_agent_path(client, alice["api_key"]["plaintext"], "alice_agent", "src")

    bob_secret = _register_integration(
        client, bob["api_key"]["plaintext"], repo_owner="bob", repo_name="repo"
    )
    # Bob has no agent paths registered.

    payload = _push_payload(
        owner="bob",
        name="repo",
        commits=[{"id": "x", "modified": ["src/anything.py"], "added": [], "removed": []}],
    )
    r = _post_webhook(client, payload, secret=bob_secret)
    assert r.status_code == 200
    # Alice's `src` path mapping must NOT trigger from Bob's push.
    assert r.json()["queued_redeploys"] == []


def test_alice_signature_does_not_authenticate_bob_repo(client, alice, bob):
    """Alice's webhook secret must never validate a webhook claiming to
    come from Bob's repo. Each repo uses its own secret."""
    alice_secret = _register_integration(
        client, alice["api_key"]["plaintext"], repo_owner="alice", repo_name="repo"
    )
    _register_integration(
        client, bob["api_key"]["plaintext"], repo_owner="bob", repo_name="repo"
    )
    payload = _push_payload(owner="bob", name="repo")
    r = _post_webhook(client, payload, secret=alice_secret)
    assert r.status_code == 401
