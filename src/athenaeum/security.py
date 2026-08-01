"""Password hashing, MCP token generation, and secret encryption.

Contract: plan §3.5/§3.6.

- Passwords: argon2id via ``argon2-cffi``; only the hash is stored.
- MCP bearer tokens: high-entropy random plaintext (shown to the user exactly
  once); only the SHA-256 hex digest is stored in ``mcp_tokens.token_hash``.
- LLM API keys at rest: Fernet symmetric encryption; the Fernet key is
  derived from ``ATHENAEUM_SECRET_KEY`` via SHA-256.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHash, VerificationError
from cryptography.fernet import Fernet

_password_hasher = PasswordHasher()

MIN_PASSWORD_LENGTH = 12  # enforced by every password-accepting WebUI handler


def hash_password(password: str) -> str:
    """Hash a plaintext password with argon2id."""
    return _password_hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """Return True iff ``password`` matches the argon2 ``password_hash``.

    Returns False (never raises) on mismatch or malformed hash.
    """
    try:
        return _password_hasher.verify(password_hash, password)
    except (VerificationError, InvalidHash):
        return False


def generate_token() -> tuple[str, str]:
    """Generate an MCP bearer token.

    Returns ``(plaintext, sha256_hex)``. The plaintext is shown to the user
    exactly once at creation; only the digest is persisted.
    """
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_token(plaintext)


def hash_token(plaintext: str) -> str:
    """Stable SHA-256 hex digest of a bearer token (the stored form)."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _fernet_key(secret_key: str) -> bytes:
    """Derive a Fernet key from the server secret (plan §3.6)."""
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


def encrypt_secret(plaintext: str, secret_key: str) -> str:
    """Encrypt a secret (e.g. an LLM API key) for storage at rest."""
    fernet = Fernet(_fernet_key(secret_key))
    return fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, secret_key: str) -> str:
    """Decrypt a value produced by :func:`encrypt_secret`."""
    fernet = Fernet(_fernet_key(secret_key))
    return fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
