"""Phase 10B.1: github_oauth module (pure OAuth helpers).

Monkeypatches httpx.post inside github_oauth so the token exchange runs
end-to-end without hitting GitHub. Mirrors test_google_oauth.py.
"""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

import github_oauth as gh


@pytest.fixture()
def _configured(monkeypatch):
    monkeypatch.setenv("LIGHTSEI_GITHUB_CLIENT_ID", "client-id-test")
    monkeypatch.setenv("LIGHTSEI_GITHUB_CLIENT_SECRET", "client-secret-test")
    monkeypatch.setenv(
        "LIGHTSEI_GITHUB_REDIRECT_URI", "https://api.test/github/oauth/callback"
    )


def test_is_configured_reads_env(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("LIGHTSEI_GITHUB_CLIENT_SECRET", raising=False)
    assert gh.is_configured() is False
    monkeypatch.setenv("LIGHTSEI_GITHUB_CLIENT_ID", "x")
    assert gh.is_configured() is False  # secret still missing
    monkeypatch.setenv("LIGHTSEI_GITHUB_CLIENT_SECRET", "y")
    assert gh.is_configured() is True


def test_new_state_is_high_entropy():
    s1, s2 = gh.new_state(), gh.new_state()
    assert s1 != s2
    assert len(s1) >= 32


def test_build_authorization_url_carries_required_params(_configured):
    url = gh.build_authorization_url(state="st-123")
    parsed = urlparse(url)
    assert parsed.scheme == "https" and parsed.netloc == "github.com"
    q = parse_qs(parsed.query)
    assert q["client_id"] == ["client-id-test"]
    assert q["redirect_uri"] == ["https://api.test/github/oauth/callback"]
    assert q["state"] == ["st-123"]
    assert "repo" in q["scope"][0]
    assert q["allow_signup"] == ["false"]


def _stub_post(monkeypatch, *, status=200, body):
    def fake_post(url, **kwargs):
        assert url == gh.GITHUB_TOKEN_URL
        # GitHub wants Accept: application/json to return a JSON body.
        assert kwargs.get("headers", {}).get("Accept") == "application/json"
        return SimpleNamespace(status_code=status, json=lambda: body)
    monkeypatch.setattr(gh.httpx, "post", fake_post)


def test_exchange_success_returns_token(_configured, monkeypatch):
    _stub_post(monkeypatch, body={"access_token": "ghu_abc123", "token_type": "bearer", "scope": "repo"})
    assert gh.exchange_code_for_token(code="the-code") == "ghu_abc123"


def test_exchange_github_error_body_raises(_configured, monkeypatch):
    # GitHub signals failure with a 200 + {error, error_description}.
    _stub_post(monkeypatch, body={"error": "bad_verification_code", "error_description": "expired"})
    with pytest.raises(gh.GitHubOAuthError):
        gh.exchange_code_for_token(code="stale")


def test_exchange_missing_token_raises(_configured, monkeypatch):
    _stub_post(monkeypatch, body={"token_type": "bearer"})  # no access_token
    with pytest.raises(gh.GitHubOAuthError):
        gh.exchange_code_for_token(code="x")


def test_exchange_http_error_raises(_configured, monkeypatch):
    _stub_post(monkeypatch, status=502, body={})
    with pytest.raises(gh.GitHubOAuthError):
        gh.exchange_code_for_token(code="x")


def test_exchange_not_configured_raises(monkeypatch):
    monkeypatch.delenv("LIGHTSEI_GITHUB_CLIENT_ID", raising=False)
    monkeypatch.delenv("LIGHTSEI_GITHUB_CLIENT_SECRET", raising=False)
    with pytest.raises(gh.GitHubOAuthError):
        gh.exchange_code_for_token(code="x")
