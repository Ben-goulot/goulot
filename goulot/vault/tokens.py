"""Vault tokens (docs/SECURITY.md, vault token).

256 bits, stored as a digest, bound to the session, sent in `X-Vault-Token`. Sliding expiry of 15 minutes
(5 to 60), extended only by `/api/private/` requests; absolute limit of 8 hours. Revoked on sleep (gap of more
than 2 minutes between wall and monotonic clocks), on lock, by `goulot vault lock` and by `goulot panic`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from goulot.db.connection import Database, get_setting
from goulot.security.audit import audit
from goulot.security.clock import Clock
from goulot.security.tokens import digest, new_token
from goulot.settings import (
    CLOCK_JUMP_THRESHOLD,
    FRESH_ASSERTION_TTL,
    VAULT_TOKEN_ABSOLUTE_TTL,
    VAULT_TOKEN_SLIDING_DEFAULT,
    VAULT_TOKEN_SLIDING_MAX,
    VAULT_TOKEN_SLIDING_MIN,
)


@dataclass(frozen=True)
class VaultToken:
    token_hash: str
    session_id: str
    expires_at: float
    last_assertion_at: float


class VaultTokenService:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        self._lock = threading.Lock()
        self._wall0 = clock.time()
        self._mono0 = clock.monotonic()

    def sliding_ttl(self) -> float:
        with self.db.connect() as conn:
            raw = get_setting(conn, "vault_lock_minutes")
        seconds = int(raw) * 60 if raw and raw.isdigit() else VAULT_TOKEN_SLIDING_DEFAULT
        return float(min(max(seconds, VAULT_TOKEN_SLIDING_MIN), VAULT_TOKEN_SLIDING_MAX))

    def check_clock(self) -> bool:
        """Detect a sleep of the Mac. Returns True (and revokes every vault token) if one happened."""
        with self._lock:
            wall, mono = self.clock.time(), self.clock.monotonic()
            drift = (wall - self._wall0) - (mono - self._mono0)
            self._wall0, self._mono0 = wall, mono
        if abs(drift) > CLOCK_JUMP_THRESHOLD:
            self.revoke_all("sleep_detected")
            return True
        return False

    def issue(self, session_id: str) -> tuple[str, float]:
        token = new_token()
        now = self.clock.time()
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM vault_tokens WHERE session_id = ?", (session_id,))
            conn.execute(
                "INSERT INTO vault_tokens (token_hash, session_id, created_at, last_used, expires_abs, "
                "last_assertion_at) VALUES (?, ?, ?, ?, ?, ?)",
                (digest(token), session_id, now, now, now + VAULT_TOKEN_ABSOLUTE_TTL, now),
            )
            audit(conn, "vault_unlocked")
        return token, self.sliding_ttl()

    def validate(self, token: str | None, session_id: str, *, extend: bool) -> VaultToken | None:
        if not token or len(token) > 128:
            return None
        now = self.clock.time()
        ttl = self.sliding_ttl()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT token_hash, session_id, last_used, expires_abs, last_assertion_at FROM vault_tokens "
                "WHERE token_hash = ?",
                (digest(token),),
            ).fetchone()
            if row is None or row["session_id"] != session_id:
                return None
            if now > row["expires_abs"] or now > row["last_used"] + ttl:
                conn.execute("DELETE FROM vault_tokens WHERE token_hash = ?", (row["token_hash"],))
                return None
            last_used = row["last_used"]
            if extend:
                conn.execute("UPDATE vault_tokens SET last_used = ? WHERE token_hash = ?", (now, row["token_hash"]))
                last_used = now
        return VaultToken(
            token_hash=row["token_hash"],
            session_id=row["session_id"],
            expires_at=min(last_used + ttl, row["expires_abs"]),
            last_assertion_at=row["last_assertion_at"],
        )

    def is_fresh(self, token: VaultToken) -> bool:
        """A signature from an existing passkey less than 2 minutes old (enrolling another passkey)."""
        return self.clock.time() - token.last_assertion_at <= FRESH_ASSERTION_TTL

    def revoke_session(self, session_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM vault_tokens WHERE session_id = ?", (session_id,))
            audit(conn, "vault_locked")

    def revoke_all(self, reason: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM vault_tokens")
            audit(conn, "vault_locked_all", reason=reason)
