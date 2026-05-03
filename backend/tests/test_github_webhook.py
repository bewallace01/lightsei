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
import base64
import hashlib
import hmac
import json
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest

import github_api
from tests.conftest import auth_headers


# Capture the real httpx.Client at module import — before any test-time
# patching. The autouse fixture below replaces httpx.Client with a
# MagicMock; without this snapshot, _mock_gh_handler would re-wrap that
# mock instead of the real class, double-patching.
_REAL_HTTPX_CLIENT = httpx.Client


# ---------- shared helpers ---------- #


def _gh_api_router(req: httpx.Request) -> httpx.Response:
    """Catch-all handler for GitHub API mocks used in this file.

    Routes by URL:
      - /repos/{owner}/{repo} → metadata (PAT validation in 10.1 path)
      - /repos/{owner}/{repo}/git/trees/{sha}?recursive=1 → 1-blob tree
      - /repos/{owner}/{repo}/git/blobs/{sha} → tiny base64 file

    The point isn't to simulate a real repo — just to give the
    fetch_directory_zip path enough to produce a non-empty zip so the
    deployment row gets created. Tests that need different shapes
    (empty repo, oversized, etc.) use _push_with_gh_mock with their
    own handler."""
    url = str(req.url)
    if "/git/trees/" in url:
        return httpx.Response(
            200,
            json={
                "sha": "tree-sha",
                "tree": [
                    {
                        "path": "polaris/bot.py",
                        "type": "blob",
                        "sha": "blob-bot",
                    },
                    {
                        "path": "bots/scout/main.py",
                        "type": "blob",
                        "sha": "blob-scout",
                    },
                    {
                        "path": "src/anything.py",
                        "type": "blob",
                        "sha": "blob-src",
                    },
                ],
                "truncated": False,
            },
        )
    if "/git/blobs/" in url:
        # Any blob — return 4 bytes of base64-encoded content. Real
        # GitHub wraps b64 at 60 chars; a single short line is fine.
        content = base64.b64encode(b"# bot\n").decode("ascii")
        return httpx.Response(
            200,
            json={"content": content, "encoding": "base64", "sha": "blob-x"},
        )
    # Default: PAT-validation shape (GET /repos/{owner}/{repo})
    return httpx.Response(
        200,
        json={
            "full_name": "acme/widgets",
            "default_branch": "main",
            "private": False,
        },
    )


@pytest.fixture(autouse=True)
def _mock_github_api_for_all_tests():
    """Auto-applied: every test in this file routes github_api.httpx
    through _gh_api_router. Any test that needs a different response
    shape uses `_mock_gh_handler(custom_handler)` to override."""
    transport = httpx.MockTransport(_gh_api_router)

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=transport, **kwargs)

    with patch.object(github_api.httpx, "Client", side_effect=factory):
        yield


@contextmanager
def _mock_pat_ok():
    """Back-compat for tests that opened this context explicitly.
    Now a no-op because the autouse fixture already covers everything."""
    yield


@contextmanager
def _mock_gh_handler(handler):
    """Per-test override: route github_api.httpx through `handler`
    instead of the default _gh_api_router. Use for tests that exercise
    error paths (404 from blob fetch, oversized tree, etc.)."""
    transport = httpx.MockTransport(handler)

    def factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return _REAL_HTTPX_CLIENT(transport=transport, **kwargs)

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
    queued = body["queued_redeploys"]
    assert len(queued) == 1
    assert queued[0]["agent_name"] == "polaris"
    assert queued[0]["commit_sha"] == "cafe1234"
    # 10.3 returns a real deployment id when fetch+create succeeds.
    assert isinstance(queued[0]["deployment_id"], str)


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


# ---------- Phase 10.3: deployment row creation ---------- #
#
# These tests assert the side effect of a successful webhook is a real
# deployment row in the DB (not just a `queued_redeploys` list in the
# response body). The Phase 5 worker's claim loop reads `deployments`
# directly, so this is the contract that closes the push-to-deploy
# loop.


def _list_deployments(client, api_key: str) -> list[dict]:
    h = auth_headers(api_key)
    r = client.get("/workspaces/me/deployments", headers=h)
    assert r.status_code == 200, r.text
    return r.json()["deployments"]


def test_push_creates_deployment_row_with_source_github_push(client, alice):
    """End-to-end: push a commit that touches polaris/, expect a new
    deployment in the DB with source='github_push' and the commit SHA."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    # No deployments before the push.
    assert _list_deployments(client, api_key) == []

    payload = _push_payload(
        after="abcdef1234567890",
        commits=[
            {
                "id": "abcdef1234567890",
                "added": [],
                "modified": ["polaris/bot.py"],
                "removed": [],
            }
        ],
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200, r.text

    deps = _list_deployments(client, api_key)
    assert len(deps) == 1
    d = deps[0]
    assert d["agent_name"] == "polaris"
    assert d["status"] == "queued"
    assert d["desired_state"] == "running"
    assert d["source"] == "github_push"
    assert d["source_commit_sha"] == "abcdef1234567890"
    assert d["source_blob_id"] is not None
    # The webhook response carries the same id.
    assert r.json()["queued_redeploys"][0]["deployment_id"] == d["id"]


def test_cli_upload_path_still_marks_source_cli(client, alice):
    """Sanity check that the existing CLI deployment path tags rows
    with source='cli' so the dashboard can distinguish them."""
    api_key = alice["api_key"]["plaintext"]
    h = auth_headers(api_key)
    # Build a tiny tar bundle. The deployment endpoint takes anything
    # non-empty; the worker only validates the bundle when it claims.
    files = {
        "agent_name": (None, "polaris"),
        "bundle": ("bundle.tar", b"\x1f\x8b\x08\x00not-real-but-nonempty"),
    }
    r = client.post("/workspaces/me/deployments", files=files, headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "cli"
    assert body["source_commit_sha"] is None


def test_push_to_two_agents_creates_two_deployments(client, alice):
    """A single push that touches both polaris/ and bots/scout/
    creates one deployment per agent — independent rows, both with
    source='github_push' and the same commit SHA."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")
    _register_agent_path(client, api_key, "scout", "bots/scout")

    payload = _push_payload(
        after="multi1deadbeef",
        commits=[
            {
                "id": "multi1deadbeef",
                "modified": ["polaris/policy.py", "bots/scout/main.py"],
                "added": [],
                "removed": [],
            }
        ],
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200

    deps = _list_deployments(client, api_key)
    by_agent = {d["agent_name"]: d for d in deps}
    assert set(by_agent) == {"polaris", "scout"}
    for d in deps:
        assert d["source"] == "github_push"
        assert d["source_commit_sha"] == "multi1deadbeef"
        assert d["status"] == "queued"


def test_push_does_not_leak_deployments_across_workspaces(client, alice, bob):
    """Bob's push must create rows only in Bob's workspace, never in
    Alice's. The Deployment.workspace_id FK is the load-bearing piece."""
    alice_secret = _register_integration(
        client, alice["api_key"]["plaintext"], repo_owner="alice", repo_name="repo"
    )
    _register_agent_path(client, alice["api_key"]["plaintext"], "polaris", "polaris")
    bob_secret = _register_integration(
        client, bob["api_key"]["plaintext"], repo_owner="bob", repo_name="repo"
    )
    _register_agent_path(client, bob["api_key"]["plaintext"], "polaris", "polaris")

    payload = _push_payload(
        owner="bob", name="repo",
        commits=[{"id": "x", "modified": ["polaris/bot.py"], "added": [], "removed": []}],
    )
    r = _post_webhook(client, payload, secret=bob_secret)
    assert r.status_code == 200

    # Alice sees nothing.
    assert _list_deployments(client, alice["api_key"]["plaintext"]) == []
    # Bob sees the new deployment.
    bob_deps = _list_deployments(client, bob["api_key"]["plaintext"])
    assert len(bob_deps) == 1
    assert bob_deps[0]["source"] == "github_push"


def test_inactive_integration_creates_no_deployment(client, alice):
    """is_active=False short-circuits the receiver before
    _queue_github_redeploy runs, so no deployment row appears."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    from sqlalchemy import update
    from db import engine
    from models import GitHubIntegration

    with engine.begin() as conn:
        conn.execute(
            update(GitHubIntegration)
            .where(GitHubIntegration.repo_owner == "acme")
            .values(is_active=False)
        )

    payload = _push_payload(
        commits=[{"id": "x", "modified": ["polaris/bot.py"], "added": [], "removed": []}]
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    assert _list_deployments(client, api_key) == []


def test_github_fetch_failure_swallowed_with_null_deployment_id(client, alice):
    """If GitHub returns 404 for the tree fetch (e.g. the commit was
    force-pushed away before we got there), the webhook stays a 200
    but the deployment_id is None and no row is created. We don't want
    GitHub retrying the webhook indefinitely on a transient error."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if "/git/trees/" in url:
            return httpx.Response(404, json={"message": "Not Found"})
        # PAT validation path (only hit on registration; this test
        # registers above the override).
        return httpx.Response(
            200,
            json={"full_name": "acme/widgets", "default_branch": "main", "private": False},
        )

    payload = _push_payload(
        commits=[{"id": "x", "modified": ["polaris/bot.py"], "added": [], "removed": []}]
    )
    with _mock_gh_handler(handler):
        r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    body = r.json()
    assert len(body["queued_redeploys"]) == 1
    assert body["queued_redeploys"][0]["deployment_id"] is None
    # And no deployment row was created.
    assert _list_deployments(client, api_key) == []


def test_zip_is_built_from_filtered_subtree(client, alice):
    """A push to polaris/ should produce a deploy zip containing files
    from polaris/, not from bots/scout/ or src/. We verify by reading
    the stored blob and unpacking the zip."""
    import io
    import zipfile
    from db import engine
    from models import DeploymentBlob

    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    payload = _push_payload(
        after="zipcheck",
        commits=[{"id": "zipcheck", "modified": ["polaris/bot.py"], "added": [], "removed": []}],
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    deployment_id = r.json()["queued_redeploys"][0]["deployment_id"]
    assert deployment_id is not None

    # Read the blob bytes back and inspect the zip's namelist.
    deps = _list_deployments(client, api_key)
    blob_id = deps[0]["source_blob_id"]
    from sqlalchemy.orm import Session as _Session
    with _Session(engine) as s:
        blob_row = s.get(DeploymentBlob, blob_id)
        assert blob_row is not None
        blob_data = blob_row.data

    with zipfile.ZipFile(io.BytesIO(blob_data)) as zf:
        names = sorted(zf.namelist())
    # The zip is rooted at the agent dir — files appear with the
    # `polaris/` prefix stripped.
    assert names == ["bot.py"]


def test_redeploy_endpoint_carries_source_forward(client, alice):
    """A user clicking 'redeploy' on a github_push deployment in the
    dashboard should produce a new deployment that's also tagged
    github_push (same blob, same commit). Otherwise the dashboard
    would falsely show a CLI deploy where there was none."""
    api_key = alice["api_key"]["plaintext"]
    h = auth_headers(api_key)
    secret = _register_integration(client, api_key)
    _register_agent_path(client, api_key, "polaris", "polaris")

    payload = _push_payload(
        after="origin42",
        commits=[{"id": "origin42", "modified": ["polaris/bot.py"], "added": [], "removed": []}],
    )
    _post_webhook(client, payload, secret=secret)
    deps = _list_deployments(client, api_key)
    assert len(deps) == 1
    original = deps[0]

    r = client.post(
        f"/workspaces/me/deployments/{original['id']}/redeploy", headers=h
    )
    assert r.status_code == 200, r.text
    new_dep = r.json()
    assert new_dep["id"] != original["id"]
    assert new_dep["source"] == "github_push"
    assert new_dep["source_commit_sha"] == "origin42"
    assert new_dep["source_blob_id"] == original["source_blob_id"]


# ---------- Phase 11.5: webhook enqueues polaris.evaluate_push ---------- #


def _post_webhook_with_delivery(
    client, payload, *, secret, delivery_id: str | None = None
):
    """Wrapper around _post_webhook that lets a test set the
    X-GitHub-Delivery header so we can verify the chain id propagates."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-GitHub-Event": "push"}
    if secret:
        headers["X-Hub-Signature-256"] = _sign(secret, body)
    if delivery_id is not None:
        headers["X-GitHub-Delivery"] = delivery_id
    return client.post("/webhooks/github", content=body, headers=headers)


def _list_polaris_commands(client, api_key: str):
    h = auth_headers(api_key)
    r = client.get("/agents/polaris/commands", headers=h)
    assert r.status_code == 200, r.text
    return r.json().get("commands", [])


def test_push_enqueues_polaris_evaluate_push_command(client, alice):
    """Every signed push the receiver accepts should leave a
    `polaris.evaluate_push` command pending for the polaris agent. The
    command's payload carries the push metadata Polaris's handler
    needs (touched_paths, commit_sha, branch, repo)."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)

    payload = _push_payload(
        after="cafe1234",
        commits=[
            {
                "id": "cafe1234",
                "added": ["polaris/new_helper.py"],
                "modified": ["backend/main.py"],
                "removed": [],
            }
        ],
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["polaris_command_id"]
    assert body["dispatch_chain_id"]

    cmds = _list_polaris_commands(client, api_key)
    assert len(cmds) == 1
    cmd = cmds[0]
    assert cmd["kind"] == "polaris.evaluate_push"
    p = cmd["payload"]
    assert p["commit_sha"] == "cafe1234"
    assert p["branch"] == "main"
    assert p["repo"] == "acme/widgets"
    assert sorted(p["touched_paths"]) == sorted(
        ["polaris/new_helper.py", "backend/main.py"]
    )


def test_push_command_chain_id_uses_github_delivery_header(client, alice):
    """Spec: the dispatched commands carry `dispatch_chain_id` matching
    the source push event id. We use the GitHub delivery uuid as that
    chain id so the entire dispatch tree (evaluate_push → atlas.run_tests
    → hermes.post) groups under one id in the 11.6 /dispatch view."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)

    delivery = "11111111-2222-3333-4444-555555555555"
    payload = _push_payload(
        commits=[{"id": "x", "added": [], "modified": ["backend/main.py"], "removed": []}]
    )
    r = _post_webhook_with_delivery(
        client, payload, secret=secret, delivery_id=delivery
    )
    assert r.status_code == 200
    assert r.json()["dispatch_chain_id"] == delivery

    cmds = _list_polaris_commands(client, api_key)
    assert cmds[0]["dispatch_chain_id"] == delivery


def test_push_with_no_paths_still_enqueues_polaris_command(client, alice):
    """Polaris's handler — not the webhook — decides whether a push is
    actionable. A push touching only docs should still produce the
    evaluate_push command (with empty / no-rule-match touched_paths);
    Polaris sees it, runs the rules, dispatches nothing."""
    api_key = alice["api_key"]["plaintext"]
    secret = _register_integration(client, api_key)

    payload = _push_payload(
        commits=[{"id": "x", "added": [], "modified": ["README.md"], "removed": []}]
    )
    r = _post_webhook(client, payload, secret=secret)
    assert r.status_code == 200
    cmds = _list_polaris_commands(client, api_key)
    assert len(cmds) == 1
    assert cmds[0]["payload"]["touched_paths"] == ["README.md"]
