"""Everything a request needs, built once per server (or per test)."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import httpx2

from goulot.db.connection import Database
from goulot.llm.backend import MessageBackend
from goulot.llm.budget import Budget
from goulot.net.allowlist import load_allowlist
from goulot.net.client import build_client
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
    budget: Budget
    # Built on demand: the API key is read from the keychain only when an AI call is about to happen.
    llm_backend: Callable[[], MessageBackend]
    http_client: Callable[[], httpx2.Client]
    jobs: ThreadPoolExecutor = field(default_factory=lambda: ThreadPoolExecutor(max_workers=1))


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
        budget=Budget(db, clock),
        llm_backend=lambda: live_backend(settings),
        http_client=lambda: build_client(load_allowlist(settings.config_dir)),
    )


class MissingAPIKey(RuntimeError):
    pass


def live_backend(settings: Settings) -> MessageBackend:
    from goulot.llm.backend import AnthropicBackend
    from goulot.llm.client import build_anthropic_client
    from goulot.security.secrets import get_secret

    key = get_secret("anthropic")
    if not key:
        raise MissingAPIKey("Clé API Anthropic absente : lancez « ./bin/goulot setup ».")
    return AnthropicBackend(build_anthropic_client(key, load_allowlist(settings.config_dir)))
