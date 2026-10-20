"""Shared fixtures. Tests never touch the real keychain nor the network (CLAUDE.md, Stack)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import keyring.backend
import pytest
from fastapi.testclient import TestClient

from goulot.api.app import create_app
from goulot.api.context import AppContext, build_context
from goulot.security.clock import ManualClock
from goulot.security.sessions import create_one_time_token
from goulot.settings import Settings
from tests.authenticator import SoftAuthenticator

ORIGIN = "http://localhost:8742"


class MemoryKeyring(keyring.backend.KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self.store:
            raise keyring.errors.PasswordDeleteError(username)
        del self.store[(service, username)]


@pytest.fixture(autouse=True)
def fake_keyring() -> Iterator[MemoryKeyring]:
    previous = keyring.get_keyring()
    ring = MemoryKeyring()
    keyring.set_keyring(ring)
    yield ring
    keyring.set_keyring(previous)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any real DNS lookup or TCP connection fails the test."""
    import socket

    def refuse(*_: object, **__: object) -> None:
        raise AssertionError("real network access attempted in a test")

    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture(autouse=True)
def no_api_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pause between two calls to a keyed API (terms of service of EIA and Census) is skipped in tests."""
    from goulot.prices import sources

    monkeypatch.setattr(sources, "PAUSE_S", 0.0)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "home", log_dir_override=tmp_path / "logs")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def ctx(settings: Settings, clock: ManualClock) -> AppContext:
    return build_context(settings, clock)


@pytest.fixture
def client(ctx: AppContext) -> Iterator[TestClient]:
    with TestClient(create_app(ctx), base_url=ORIGIN, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture
def authenticator() -> SoftAuthenticator:
    return SoftAuthenticator(origin=ORIGIN)


def db_bytes(db_path: Path) -> bytes:
    """Raw bytes of the database file and of its WAL and shared-memory files, when present."""
    return b"".join(
        Path(str(db_path) + suffix).read_bytes()
        for suffix in ("", "-wal", "-shm")
        if Path(str(db_path) + suffix).exists()
    )


def login(client: TestClient, ctx: AppContext) -> str:
    """Open a session through a launch link; returns the CSRF token."""
    token = create_one_time_token(ctx.db, ctx.clock, "login")
    r = client.post("/auth/exchange", json={"token": token}, headers={"Origin": ORIGIN})
    assert r.status_code == 200, r.text
    csrf: str = client.get("/api/session").json()["csrf_token"]
    return csrf


def unsafe_headers(csrf: str, vault_token: str | None = None) -> dict[str, str]:
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    if vault_token:
        headers["X-Vault-Token"] = vault_token
    return headers


def setup_vault(ctx: AppContext) -> str:
    """Create the vault; returns the recovery key."""
    recovery_key = ctx.vault.create()
    ctx.vault.mark_recovery_confirmed()
    return recovery_key


def enroll(client: TestClient, ctx: AppContext, csrf: str, auth: SoftAuthenticator) -> None:
    grant = create_one_time_token(ctx.db, ctx.clock, "enroll")
    r = client.post("/api/vault/passkeys/register/options", json={"grant": grant}, headers=unsafe_headers(csrf))
    assert r.status_code == 200, r.text
    credential = auth.register(r.json()["options"])
    r = client.post(
        "/api/vault/passkeys/register/verify",
        json={"credential": credential, "label": "test", "grant": grant},
        headers=unsafe_headers(csrf),
    )
    assert r.status_code == 200, r.text


def unlock(client: TestClient, csrf: str, auth: SoftAuthenticator) -> str:
    r = client.post("/api/vault/unlock/options", json={}, headers=unsafe_headers(csrf))
    assert r.status_code == 200, r.text
    r = client.post(
        "/api/vault/unlock/verify", json={"credential": auth.assert_(r.json()["options"])}, headers=unsafe_headers(csrf)
    )
    assert r.status_code == 200, r.text
    token: str = r.json()["vault_token"]
    return token


@pytest.fixture
def opened(client: TestClient, ctx: AppContext, authenticator: SoftAuthenticator) -> tuple[str, str]:
    """Logged in, vault created, one passkey, vault open. Returns (csrf, vault_token)."""
    setup_vault(ctx)
    csrf = login(client, ctx)
    enroll(client, ctx, csrf, authenticator)
    return csrf, unlock(client, csrf, authenticator)
