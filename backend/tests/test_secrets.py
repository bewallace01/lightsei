"""Workspace secrets store: encrypted-at-rest KV.

Threat model: anyone with a workspace credential can read every secret in the
workspace. The store protects against:
  - DB-only compromise (rows are encrypted)
  - Cross-workspace access (foreign-key + scoping)
  - Accidental leakage in /list (values never returned there)

It does NOT protect against a stolen session/api_key — that's handled by the
auth layer's revocation surface.
"""
import os

import pytest
from sqlalchemy import text

import secrets_crypto
from db import engine
from tests.conftest import auth_headers


def test_set_then_get_roundtrips_value(client, alice):
    h = auth_headers(alice["session_token"])

    r = client.put(
        "/workspaces/me/secrets/OPENAI_API_KEY",
        json={"value": "sk-proj-pretend-this-is-real"},
        headers=h,
    )
    assert r.status_code == 200
    assert r.json()["name"] == "OPENAI_API_KEY"

    r = client.get("/workspaces/me/secrets/OPENAI_API_KEY", headers=h)
    assert r.status_code == 200
    assert r.json()["value"] == "sk-proj-pretend-this-is-real"


def test_list_never_returns_value(client, alice):
    h = auth_headers(alice["session_token"])
    client.put(
        "/workspaces/me/secrets/A",
        json={"value": "secret-a"}, headers=h,
    )
    client.put(
        "/workspaces/me/secrets/B",
        json={"value": "secret-b"}, headers=h,
    )

    r = client.get("/workspaces/me/secrets", headers=h)
    body = r.json()
    assert {s["name"] for s in body["secrets"]} == {"A", "B"}
    for s in body["secrets"]:
        assert "value" not in s
        assert "encrypted_value" not in s


def test_value_is_actually_encrypted_in_db(client, alice):
    """If someone steals a DB dump, the plaintext must not be visible."""
    h = auth_headers(alice["session_token"])
    plaintext = "this-is-the-secret-12345"
    client.put(
        "/workspaces/me/secrets/X",
        json={"value": plaintext}, headers=h,
    )

    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT encrypted_value FROM workspace_secrets WHERE name = 'X'")
        ).all()
    assert len(rows) == 1
    blob = rows[0][0]
    assert plaintext not in blob, "plaintext leaked into the encrypted_value column"


def test_update_overwrites_value(client, alice):
    h = auth_headers(alice["session_token"])
    client.put(
        "/workspaces/me/secrets/X", json={"value": "v1"}, headers=h,
    )
    client.put(
        "/workspaces/me/secrets/X", json={"value": "v2"}, headers=h,
    )
    r = client.get("/workspaces/me/secrets/X", headers=h)
    assert r.json()["value"] == "v2"


def test_delete_removes(client, alice):
    h = auth_headers(alice["session_token"])
    client.put(
        "/workspaces/me/secrets/X", json={"value": "v"}, headers=h,
    )
    r = client.delete("/workspaces/me/secrets/X", headers=h)
    assert r.status_code == 200
    r = client.get("/workspaces/me/secrets/X", headers=h)
    assert r.status_code == 404


def test_cross_workspace_isolation(client, alice, bob):
    h_a = auth_headers(alice["session_token"])
    h_b = auth_headers(bob["session_token"])
    client.put(
        "/workspaces/me/secrets/SHARED_NAME",
        json={"value": "alice-value"}, headers=h_a,
    )

    # Bob can have a secret with the same name without conflict.
    r = client.put(
        "/workspaces/me/secrets/SHARED_NAME",
        json={"value": "bob-value"}, headers=h_b,
    )
    assert r.status_code == 200

    # Each side reads its own value.
    assert client.get(
        "/workspaces/me/secrets/SHARED_NAME", headers=h_a,
    ).json()["value"] == "alice-value"
    assert client.get(
        "/workspaces/me/secrets/SHARED_NAME", headers=h_b,
    ).json()["value"] == "bob-value"

    # Bob's list does NOT contain alice's row even though alice was first.
    r = client.get("/workspaces/me/secrets", headers=h_b)
    assert len(r.json()["secrets"]) == 1


def test_invalid_name_400(client, alice):
    h = auth_headers(alice["session_token"])
    for bad in ("1leading-digit", "with space", "with-dash", "with.dot", ""):
        r = client.put(
            f"/workspaces/me/secrets/{bad or '_empty_'}",
            json={"value": "v"}, headers=h,
        )
        # FastAPI itself rejects empty path segments (404). The regex catches
        # the rest.
        assert r.status_code in (400, 404), f"name={bad!r} -> {r.status_code}"


def test_unknown_name_404(client, alice):
    h = auth_headers(alice["session_token"])
    r = client.get("/workspaces/me/secrets/NOT_THERE", headers=h)
    assert r.status_code == 404


def test_unauthenticated_blocked(client):
    assert client.get("/workspaces/me/secrets").status_code == 401
    assert client.get("/workspaces/me/secrets/X").status_code == 401
    assert client.put(
        "/workspaces/me/secrets/X", json={"value": "v"},
    ).status_code == 401


def test_503_when_master_key_missing(client, alice, monkeypatch):
    """Fail closed: if LIGHTSEI_SECRETS_KEY isn't configured, write/read
    return 503 rather than silently using a default."""
    monkeypatch.delenv("LIGHTSEI_SECRETS_KEY", raising=False)
    h = auth_headers(alice["session_token"])

    r = client.put(
        "/workspaces/me/secrets/X", json={"value": "v"}, headers=h,
    )
    assert r.status_code == 503

    # GET also blocks, even on a row written under a previous (now-missing) key.
    r = client.get("/workspaces/me/secrets/anything", headers=h)
    assert r.status_code == 503


def test_crypto_unit_roundtrip():
    """Direct encrypt/decrypt roundtrip — independent of the API."""
    blob = secrets_crypto.encrypt("hello-world")
    assert secrets_crypto.decrypt(blob) == "hello-world"
    assert blob != "hello-world"


def test_crypto_unit_tamper_detected():
    """AES-GCM authenticates: flipping a byte must fail decryption."""
    import base64
    blob = secrets_crypto.encrypt("hello-world")
    raw = base64.b64decode(blob)
    # Flip one bit of the ciphertext.
    tampered_raw = raw[:15] + bytes([raw[15] ^ 1]) + raw[16:]
    tampered = base64.b64encode(tampered_raw).decode()
    with pytest.raises(Exception):
        secrets_crypto.decrypt(tampered)
