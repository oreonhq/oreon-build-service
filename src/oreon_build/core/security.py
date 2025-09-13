"""Password and token hashing."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Optional

import bcrypt

# Bcrypt has a 72-byte limit; truncate to avoid ValueError
_MAX_BCRYPT_BYTES = 72


def _to_bcrypt_input(plain: str) -> bytes:
    b = plain.encode("utf-8")
    return b[: _MAX_BCRYPT_BYTES] if len(b) > _MAX_BCRYPT_BYTES else b


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(_to_bcrypt_input(plain), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_to_bcrypt_input(plain), hashed.encode("ascii"))
    except Exception:
        return False


def hash_worker_token(plain: str, secret: str) -> str:
    """Legacy: hash enrollment token with server secret."""
    return hashlib.sha256((secret + ":" + plain).encode()).hexdigest()


def verify_worker_token(plain: str, secret: str, stored_hash: str) -> bool:
    """Verify either a stored bcrypt hash (per-worker token) or legacy hash."""
    if stored_hash.startswith("$2"):
        try:
            return bcrypt.checkpw(_to_bcrypt_input(plain), stored_hash.encode("ascii"))
        except Exception:
            return False
    return hmac.compare_digest(hash_worker_token(plain, secret), stored_hash)


def generate_enrollment_token() -> str:
    return secrets.token_urlsafe(32)
