"""Everything a request needs, built once per server (or per test)."""

from __future__ import annotations

from dataclasses import dataclass

from goulot.db.connection import Database
from goulot.security.clock import Clock, SystemClock
from goulot.security.fs import ensure_private_dir
from goulot.security.ratelimit import RateLimiter
from goulot.security.sessions import SessionManager
from goulot.settings import API_RATE_PER_MINUTE, AUTH_RATE_PER_MINUTE, Settings
from goulot.vault.keys import Vault
from goulot.vault.passkeys import PasskeyService
from goulot.vault.tokens import VaultTokenService


@dataclass
class AppContext:
    settings: Settings
    db: Database
    clock: Clock
    sessions: SessionManager
    vault: Vault
    vault_tokens: VaultTokenService
    passkeys: PasskeyService
    limiter: RateLimiter


def build_context(settings: Settings, clock: Clock | None = None) -> AppContext:
    clock = clock or SystemClock()
    ensure_private_dir(settings.home)
    db = Database(settings.db_path)
    db.migrate()
    return AppContext(
        settings=settings,
        db=db,
        clock=clock,
        sessions=SessionManager(db, clock),
        vault=Vault(db),
        vault_tokens=VaultTokenService(db, clock),
        passkeys=PasskeyService(db, clock, settings.origin),
        limiter=RateLimiter(clock, {"auth": AUTH_RATE_PER_MINUTE, "api": API_RATE_PER_MINUTE}),
    )
