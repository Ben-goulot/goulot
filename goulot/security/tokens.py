"""Random tokens, stored only as SHA-256 digests."""

from __future__ import annotations

import hashlib
import hmac
import secrets

TOKEN_BYTES = 32  # 256 bits


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
