"""One-time launch links, session cookies and CSRF tokens (docs/SECURITY.md, server and session).

`goulot open` stores the SHA-256 of a 256-bit launch token valid 60 s; the page exchanges it once for a
session cookie. Sessions are stored as digests, with a 7-day sliding and a 30-day absolute expiry.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import uuid
from dataclasses import dataclass

from goulot.db.connection import Database
from goulot.security.audit import audit
from goulot.security.clock import Clock
from goulot.security.tokens import digest, new_token
from goulot.settings import ENROLL_GRANT_TTL, LAUNCH_TOKEN_TTL, SESSION_ABSOLUTE_TTL, SESSION_SLIDING_TTL

COOKIE_NAME = "goulot_session"
_LAST_SEEN_GRANULARITY = 60.0


@dataclass(frozen=True)
class Session:
    id: str
    created_at: float


def create_one_time_token(db: Database, clock: Clock, purpose: str) -> str:
    ttl = LAUNCH_TOKEN_TTL if purpose == "login" else ENROLL_GRANT_TTL
    token = new_token()
    now = clock.time()
    with db.transaction() as conn:
        conn.execute("DELETE FROM launch_tokens WHERE expires_at < ?", (now - 3600,))
        conn.execute(
            "INSERT INTO launch_tokens (token_hash, purpose, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (digest(token), purpose, now, now + ttl),
        )
    return token


def consume_one_time_token(conn: sqlite3.Connection, token: str, purpose: str, now: float) -> bool:
    """Single use: the row is marked used in the same transaction that checks it."""
    cur = conn.execute(
        "UPDATE launch_tokens SET used_at = ? WHERE token_hash = ? AND purpose = ? AND used_at IS NULL "
        "AND expires_at >= ?",
        (now, digest(token), purpose, now),
    )
    return cur.rowcount == 1


class SessionManager:
    def __init__(self, db: Database, clock: Clock) -> None:
        self.db = db
        self.clock = clock
        # CSRF secret drawn at server start: a restart invalidates old CSRF tokens (docs/DECISIONS.md).
        self._csrf_key = secrets.token_bytes(32)

    def exchange(self, launch_token: str) -> str | None:
        """Trade a valid launch token for a new session token. None if invalid, expired or already used."""
        now = self.clock.time()
        with self.db.transaction() as conn:
            if not consume_one_time_token(conn, launch_token, "login", now):
                audit(conn, "auth_exchange_failed")
                return None
            token = new_token()
            conn.execute(
                "INSERT INTO sessions (id, token_hash, created_at, last_seen, expires_abs) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), digest(token), now, now, now + SESSION_ABSOLUTE_TTL),
            )
            audit(conn, "session_created")
        return token

    def validate(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        now = self.clock.time()
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT id, created_at, last_seen, expires_abs FROM sessions WHERE token_hash = ?", (digest(token),)
            ).fetchone()
            if row is None:
                return None
            if now > row["expires_abs"] or now > row["last_seen"] + SESSION_SLIDING_TTL:
                conn.execute("DELETE FROM sessions WHERE id = ?", (row["id"],))
                return None
            if now - row["last_seen"] > _LAST_SEEN_GRANULARITY:
                conn.execute("UPDATE sessions SET last_seen = ? WHERE id = ?", (now, row["id"]))
        return Session(id=row["id"], created_at=row["created_at"])

    def csrf_token(self, session: Session) -> str:
        return hmac.new(self._csrf_key, session.id.encode("utf-8"), hashlib.sha256).hexdigest()

    def check_csrf(self, session: Session, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(self.csrf_token(session), candidate or "")

    def revoke(self, session_id: str) -> None:
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            conn.execute("DELETE FROM vault_tokens WHERE session_id = ?", (session_id,))


def revoke_everything(db: Database) -> None:
    """`goulot panic`: every session, launch token, enrollment grant and vault token."""
    with db.transaction() as conn:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM launch_tokens")
        conn.execute("DELETE FROM vault_tokens")
        conn.execute("DELETE FROM vault_challenges")
        audit(conn, "panic_revoke_all")
