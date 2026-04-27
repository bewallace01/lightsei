"""Encryption-at-rest for workspace secrets.

Algorithm: AES-256-GCM via the cryptography library. 12-byte random nonce per
secret, 16-byte authentication tag. Storage format is base64(nonce || ciphertext_with_tag).

Master key: read from LIGHTSEI_SECRETS_KEY env var as base64-encoded 32 bytes.
If unset, encrypt/decrypt raise SecretsUnavailable so the API can return 503
rather than silently fall through to a default key. Generate with:

    python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"

Rotation is not built. Adding a key version prefix to ciphertext rows would
make rotation possible without re-encrypting all existing rows at once.
"""
import base64
import os
import secrets as _secrets
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretsUnavailable(Exception):
    """Raised when LIGHTSEI_SECRETS_KEY is missing or malformed."""


_NONCE_BYTES = 12


def _master_key() -> bytes:
    raw = os.environ.get("LIGHTSEI_SECRETS_KEY")
    if not raw:
        raise SecretsUnavailable(
            "LIGHTSEI_SECRETS_KEY env var is not set; secrets cannot be "
            "encrypted or decrypted. Generate one with `python -c \"import "
            "secrets, base64; print(base64.b64encode(secrets.token_bytes(32))"
            ".decode())\"`."
        )
    try:
        key = base64.b64decode(raw)
    except Exception as e:
        raise SecretsUnavailable(f"LIGHTSEI_SECRETS_KEY is not valid base64: {e}")
    if len(key) != 32:
        raise SecretsUnavailable(
            f"LIGHTSEI_SECRETS_KEY must decode to 32 bytes, got {len(key)}"
        )
    return key


def is_available() -> bool:
    try:
        _master_key()
        return True
    except SecretsUnavailable:
        return False


def encrypt(plaintext: str) -> str:
    """Encrypt and return the base64-encoded ciphertext (nonce prefix included)."""
    key = _master_key()
    aead = AESGCM(key)
    nonce = _secrets.token_bytes(_NONCE_BYTES)
    ct = aead.encrypt(nonce, plaintext.encode("utf-8"), associated_data=None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(blob: str) -> str:
    """Decrypt a value previously produced by encrypt(). Raises on tamper or
    wrong key."""
    key = _master_key()
    raw = base64.b64decode(blob)
    if len(raw) < _NONCE_BYTES + 16:  # 16 = AES-GCM tag length
        raise ValueError("ciphertext too short")
    nonce, ct = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    aead = AESGCM(key)
    return aead.decrypt(nonce, ct, associated_data=None).decode("utf-8")
