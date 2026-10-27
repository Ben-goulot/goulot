"""Threats 24 to 29: vault, encryption at rest, passkeys, recovery (docs/SECURITY.md)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import uuid
from pathlib import Path

import keyring
import pytest
from fastapi.testclient import TestClient

from goulot.api.context import AppContext
from goulot.db.backup import backup
from goulot.db.connection import MIGRATIONS_DIR, Database
from goulot.private import coverage
from goulot.security.logs import setup_logging
from goulot.security.sessions import create_one_time_token, revoke_everything
from goulot.vault import recovery
from goulot.vault.crypto import DecryptionError, decrypt_field, encrypt_field
from goulot.vault.keys import Vault, VaultError
from tests.authenticator import SoftAuthenticator
from tests.conftest import (
    ORIGIN,
    MemoryKeyring,
    api_routes,
    db_bytes,
    enroll,
    fill_path,
    login,
    setup_vault,
    unlock,
    unsafe_headers,
)

# Canary values recognisable byte for byte (docs/SPEC.md, section 13).
CANARY_TEXT = ["CANARY-SITE-7F3A", "CANARY-CITY-9Q2X"]
CANARY_COVERAGE = {"stock_weeks": 37.2917, "supplier_lead_time_days": 417, "price_threshold_pct": 63.4173}


def _put_canaries(client: TestClient, ctx: AppContext, csrf: str, vault_token: str) -> None:
    r = client.put("/api/private/coverage/acrylonitrile", json=CANARY_COVERAGE, headers=unsafe_headers(csrf, vault_token))
    assert r.status_code == 200
    # A text canary through the same encryption path (Ma chaîne arrives in phase 4).
    record = str(uuid.uuid4())
    blob, version = ctx.vault.encrypt_json(coverage.FIELD, record, {"site": CANARY_TEXT[0], "city": CANARY_TEXT[1]})
    with ctx.db.transaction() as conn:
        conn.execute(
            "INSERT INTO user_coverage (id, material_ref, payload, key_version, created_at, updated_at) "
            "VALUES (?, 'canary-text', ?, ?, 'now', 'now')",
            (record, blob, version),
        )


def _assert_no_canary(raw: bytes, where: str) -> None:
    for text in CANARY_TEXT:
        assert text.encode() not in raw, (where, text)
    for number in ("37.2917", "63.4173", '"supplier_lead_time_days":417'):
        assert number.encode() not in raw, (where, number)


def test_private_data_encrypted_at_rest(client: TestClient, ctx: AppContext, opened: tuple[str, str]) -> None:
    csrf, vault_token = opened
    log_file = setup_logging(ctx.settings.log_dir)
    with ctx.db.connect() as holder:  # keeps the WAL alive: the canaries must not be in it either
        holder.execute("SELECT 1")
        _put_canaries(client, ctx, csrf, vault_token)
        wal = Path(f"{ctx.settings.db_path}-wal")
        assert wal.exists() and wal.stat().st_size > 0
        _assert_no_canary(db_bytes(ctx.settings.db_path), "database and WAL before checkpoint")
        backup_file = backup(ctx.db, ctx.settings.backup_dir)
        holder.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _assert_no_canary(db_bytes(ctx.settings.db_path), "database after checkpoint")
    _assert_no_canary(backup_file.read_bytes(), "backup")
    _assert_no_canary(log_file.read_bytes(), "log")
    # Deleting a record leaves nothing behind (secure_delete), and the values do read back through the API.
    listed = client.get("/api/private/coverage", headers={"X-Vault-Token": vault_token}).json()
    assert {k: v for k, v in listed[0].items() if k != "material_ref"} == CANARY_COVERAGE
    with ctx.db.connect() as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    # The master key is in the keychain, never in the database or a backup.
    master = keyring.get_password("goulot-vault", "master-key")
    assert master and master.encode() not in db_bytes(ctx.settings.db_path) + backup_file.read_bytes()


def test_ciphertext_bound_to_slot(ctx: AppContext) -> None:
    key = bytes(range(32))
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    blob = encrypt_field(key, "coverage.v1", a, 1, b"secret")
    assert decrypt_field(key, "coverage.v1", a, 1, blob) == b"secret"
    assert encrypt_field(key, "coverage.v1", a, 1, b"secret") != blob  # random nonce
    for field, record, version in (("coverage.v1", b, 1), ("private_chain.v1", a, 1), ("coverage.v1", a, 2)):
        with pytest.raises(DecryptionError):
            decrypt_field(key, field, record, version, blob)
    tampered = blob[:-1] + bytes([blob[-1] ^ 1])
    with pytest.raises(DecryptionError):
        decrypt_field(key, "coverage.v1", a, 1, tampered)
    with pytest.raises(ValueError):
        encrypt_field(key, "Table.Column", a, 1, b"x")  # logical names only
    # Swapping two ciphertexts between records in the database breaks both.
    setup_vault(ctx)
    coverage.put(ctx.db, ctx.vault, "mat-a", coverage.Coverage(stock_weeks=1))
    coverage.put(ctx.db, ctx.vault, "mat-b", coverage.Coverage(stock_weeks=2))
    with ctx.db.transaction() as conn:
        rows = conn.execute("SELECT id, payload FROM user_coverage ORDER BY material_ref").fetchall()
        conn.execute("UPDATE user_coverage SET payload = ? WHERE id = ?", (rows[1]["payload"], rows[0]["id"]))
        conn.execute("UPDATE user_coverage SET payload = ? WHERE id = ?", (rows[0]["payload"], rows[1]["id"]))
    with pytest.raises(DecryptionError):
        coverage.list_all(ctx.db, ctx.vault)


def test_encrypted_fields_survive_migration(ctx: AppContext, tmp_path: Path) -> None:
    setup_vault(ctx)
    expected = {f"mat-{i}": coverage.Coverage(stock_weeks=i, supplier_lead_time_days=10 + i) for i in range(5)}
    for ref, cov in expected.items():
        coverage.put(ctx.db, ctx.vault, ref, cov)
    migrations = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, migrations)
    # A migration that rebuilds the table (new rowids, new column order) moves ciphertexts as they are.
    (migrations / "9999_rebuild_coverage.sql").write_text(
        "CREATE TABLE user_coverage_new (material_ref TEXT NOT NULL UNIQUE, note TEXT, id TEXT PRIMARY KEY, "
        "key_version INTEGER NOT NULL, payload BLOB NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);\n"
        "INSERT INTO user_coverage_new (material_ref, id, key_version, payload, created_at, updated_at) "
        "SELECT material_ref, id, key_version, payload, created_at, updated_at FROM user_coverage ORDER BY id DESC;\n"
        "DROP TABLE user_coverage;\nALTER TABLE user_coverage_new RENAME TO user_coverage;\n",
        encoding="utf-8",
    )
    assert ctx.db.migrate(migrations) == [9999]
    with ctx.db.connect() as conn:
        conn.execute("VACUUM")
    got = {row["material_ref"]: row for row in coverage.list_all(ctx.db, ctx.vault)}
    assert got.keys() == expected.keys()
    for ref, cov in expected.items():
        assert {k: v for k, v in got[ref].items() if k != "material_ref"} == cov.model_dump()
    assert coverage.verify_all(ctx.db, ctx.vault) == 5


def test_inputs_digest_is_keyed(ctx: AppContext, tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    setup_vault(ctx)
    inputs = {"stock_weeks": 6, "supplier_lead_time_days": 28, "threshold_pct": 5}
    digest = ctx.vault.inputs_digest(inputs)
    assert digest == ctx.vault.inputs_digest(dict(reversed(list(inputs.items()))))  # canonical
    canonical = json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    assert digest != hashlib.sha256(canonical).hexdigest()
    # Trying every plausible value with a plain hash finds nothing.
    plain = {
        hashlib.sha256(
            json.dumps({"stock_weeks": s, "supplier_lead_time_days": d, "threshold_pct": t}, sort_keys=True,
                       separators=(",", ":")).encode()
        ).hexdigest()
        for s in range(13) for d in range(0, 60, 7) for t in range(11)
    }
    assert digest not in plain
    # Another vault (another key) gives another digest for the same inputs.
    fake_keyring.store.clear()
    other_db = Database(tmp_path / "other" / "goulot.sqlite")
    other_db.migrate()
    other = Vault(other_db)
    other.create()
    assert other.inputs_digest(inputs) != digest


def test_vault_recovery_roundtrip(ctx: AppContext, tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    recovery_key = setup_vault(ctx)
    coverage.put(ctx.db, ctx.vault, "acrylonitrile", coverage.Coverage(**CANARY_COVERAGE))
    copy = backup(ctx.db, ctx.settings.backup_dir)
    restored_path = tmp_path / "new-mac" / "goulot.sqlite"
    restored_path.parent.mkdir()
    shutil.copy(copy, restored_path)
    fake_keyring.store.clear()  # a new Mac: empty keychain
    restored_db = Database(restored_path)
    restored = Vault(restored_db)
    with pytest.raises(VaultError):
        coverage.list_all(restored_db, restored)
    wrong = recovery.encode(recovery.generate())
    with pytest.raises(VaultError):
        restored.recover(wrong)
    typo = recovery_key[:-2] + ("0" if recovery_key[-2] != "0" else "1") + recovery_key[-1]
    with pytest.raises(recovery.RecoveryKeyError):
        restored.recover(typo)  # caught by the check symbol
    restored.recover(recovery_key.lower().replace("-", " "))  # case and separators do not matter
    items = coverage.list_all(restored_db, restored)
    assert {k: v for k, v in items[0].items() if k != "material_ref"} == CANARY_COVERAGE
    # A new recovery key invalidates the old one.
    new_key = restored.new_recovery_key()
    assert not restored.check_recovery_key(recovery_key)
    assert restored.check_recovery_key(new_key)
    assert not restored.recovery_confirmed()
    # Recovering on a Mac that holds another master key is refused unless asked explicitly.
    fake_keyring.store[("goulot-vault", "master-key")] = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE="
    with pytest.raises(VaultError):
        restored.recover(new_key)
    restored.recover(new_key, replace_existing=True)
    assert coverage.verify_all(restored_db, restored) == 1


def _private_routes(client: TestClient) -> list[tuple[str, str]]:
    return [(m, r.path) for r in api_routes(client.app) if r.path.startswith("/api/private/") for m in r.methods]


def test_private_routes_require_vault(
    client: TestClient, ctx: AppContext, opened: tuple[str, str], authenticator: SoftAuthenticator
) -> None:
    csrf, vault_token = opened
    routes = _private_routes(client)
    assert len(routes) >= 20

    def call(method: str, path: str, token: str | None) -> int:
        path = fill_path(path, {"passkey_id": "unknown", "ref": "acrylonitrile"})
        body = {"minutes": 15} if path.endswith("lock-minutes") else {}
        return client.request(method, path, json=body, headers=unsafe_headers(csrf, token)).status_code

    for method, path in routes:
        assert call(method, path, None) == 403, (method, path)
        assert call(method, path, "forged" * 8) == 403, (method, path)
        assert call(method, path, vault_token) != 403, (method, path)
    # A vault token is bound to its session: another session cannot use it.
    other = TestClient(client.app, base_url=ORIGIN)
    other_csrf = login(other, ctx)
    r = other.get("/api/private/coverage", headers={"X-Vault-Token": vault_token, "X-CSRF-Token": other_csrf})
    assert r.status_code == 403
    # The closed vault says nothing more than « Coffre fermé ».
    r = client.get("/api/private/coverage")
    assert r.json() == {"detail": "Coffre fermé."}


def test_vault_token_expiry(
    client: TestClient, ctx: AppContext, opened: tuple[str, str], authenticator: SoftAuthenticator
) -> None:
    csrf, token = opened
    clock = ctx.clock

    def private(t: str) -> int:
        return client.get("/api/private/coverage", headers={"X-Vault-Token": t}).status_code

    clock.advance(14 * 60)  # type: ignore[attr-defined]
    assert private(token) == 200  # a user action extends the sliding window
    clock.advance(14 * 60)  # type: ignore[attr-defined]
    assert private(token) == 200
    clock.advance(16 * 60)  # type: ignore[attr-defined]
    assert private(token) == 403  # 15 minutes without action
    # Absolute limit: 8 hours even with constant activity.
    token = unlock(client, csrf, authenticator)
    for _ in range(47):
        clock.advance(10 * 60)  # type: ignore[attr-defined]
        assert private(token) == 200
    clock.advance(11 * 60)  # type: ignore[attr-defined]
    assert private(token) == 403
    # Sleep of the Mac: the wall clock jumps, the monotonic clock does not.
    token = unlock(client, csrf, authenticator)
    clock.sleep_jump(3 * 60)  # type: ignore[attr-defined]
    assert private(token) == 403
    # Padlock, `goulot vault lock` and `goulot panic`.
    token = unlock(client, csrf, authenticator)
    assert client.post("/api/vault/lock", json={}, headers=unsafe_headers(csrf)).status_code == 200
    assert private(token) == 403
    token = unlock(client, csrf, authenticator)
    ctx.vault_tokens.revoke_all("cli")
    assert private(token) == 403
    token = unlock(client, csrf, authenticator)
    revoke_everything(ctx.db)
    assert private(token) == 401  # panic ended the session itself
    with ctx.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM vault_tokens").fetchone()[0] == 0
    # Configurable from 5 to 60 minutes.
    csrf = login(client, ctx)
    token = unlock(client, csrf, authenticator)
    r = client.put("/api/private/vault/lock-minutes", json={"minutes": 61}, headers=unsafe_headers(csrf, token))
    assert r.status_code == 422
    r = client.put("/api/private/vault/lock-minutes", json={"minutes": 5}, headers=unsafe_headers(csrf, token))
    assert r.status_code == 200
    clock.advance(6 * 60)  # type: ignore[attr-defined]
    assert private(token) == 403


def test_poll_does_not_extend_vault_token(
    client: TestClient, ctx: AppContext, opened: tuple[str, str]
) -> None:
    _, token = opened
    for minute in range(1, 15):
        ctx.clock.advance(60)  # type: ignore[attr-defined]
        status = client.get("/api/vault/status", headers={"X-Vault-Token": token}).json()
        assert status["unlocked"] is True, minute
        client.get("/api/session")
        client.get("/api/settings/secrets")
    ctx.clock.advance(61)  # type: ignore[attr-defined]
    assert client.get("/api/vault/status", headers={"X-Vault-Token": token}).json()["unlocked"] is False
    assert client.get("/api/private/coverage", headers={"X-Vault-Token": token}).status_code == 403


def test_unlock_rate_limited(
    client: TestClient, ctx: AppContext, opened: tuple[str, str], authenticator: SoftAuthenticator
) -> None:
    csrf, _ = opened
    intruder = SoftAuthenticator(origin=ORIGIN)
    intruder.credential_id = authenticator.credential_id  # right credential id, wrong private key
    for _ in range(5):
        options = client.post("/api/vault/unlock/options", json={}, headers=unsafe_headers(csrf)).json()["options"]
        r = client.post(
            "/api/vault/unlock/verify", json={"credential": intruder.assert_(options)}, headers=unsafe_headers(csrf)
        )
        assert r.status_code == 403
    r = client.post("/api/vault/unlock/options", json={}, headers=unsafe_headers(csrf))
    assert r.status_code == 429
    assert client.get("/api/vault/status").json()["blocked_for"] > 14 * 60
    with ctx.db.connect() as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM audit_log")]
    assert kinds.count("vault_unlock_failed") == 5 and "vault_unlock_blocked" in kinds
    ctx.clock.advance(15 * 60 + 1)  # type: ignore[attr-defined]
    assert unlock(client, csrf, authenticator)
    # A replayed challenge, a counter going backwards, another origin and a missing user verification all fail.
    options = client.post("/api/vault/unlock/options", json={}, headers=unsafe_headers(csrf)).json()["options"]
    good = authenticator.assert_(options)
    assert client.post("/api/vault/unlock/verify", json={"credential": good}, headers=unsafe_headers(csrf)).status_code == 200
    assert client.post("/api/vault/unlock/verify", json={"credential": good}, headers=unsafe_headers(csrf)).status_code == 403
    for credential_maker in (
        lambda o: authenticator.assert_(o, sign_count=1),
        lambda o: authenticator.assert_(o, origin="http://localhost:9000"),
    ):
        options = client.post("/api/vault/unlock/options", json={}, headers=unsafe_headers(csrf)).json()["options"]
        r = client.post(
            "/api/vault/unlock/verify", json={"credential": credential_maker(options)}, headers=unsafe_headers(csrf)
        )
        assert r.status_code == 403
    authenticator.user_verified = False
    options = client.post("/api/vault/unlock/options", json={}, headers=unsafe_headers(csrf)).json()["options"]
    r = client.post("/api/vault/unlock/verify", json={"credential": authenticator.assert_(options)}, headers=unsafe_headers(csrf))
    assert r.status_code == 403


def test_passkey_enrollment_requires_proof(client: TestClient, ctx: AppContext) -> None:
    setup_vault(ctx)
    csrf = login(client, ctx)
    first = SoftAuthenticator(origin=ORIGIN)

    def options(grant: str | None, token: str | None = None) -> int:
        return client.post(
            "/api/vault/passkeys/register/options", json={"grant": grant}, headers=unsafe_headers(csrf, token)
        ).status_code

    assert options(None) == 403  # first passkey: no proof, no enrollment
    assert options("not-a-real-grant-value-xxxxxxxxxxxxxxxxxxxxx") == 403
    login_token = create_one_time_token(ctx.db, ctx.clock, "login")
    assert options(login_token) == 403  # a login token is not an enrollment grant
    expired = create_one_time_token(ctx.db, ctx.clock, "enroll")
    ctx.clock.advance(10 * 60 + 1)  # type: ignore[attr-defined]
    assert options(expired) == 403

    grant = create_one_time_token(ctx.db, ctx.clock, "enroll")
    r = client.post("/api/vault/passkeys/register/options", json={"grant": grant}, headers=unsafe_headers(csrf))
    assert r.status_code == 200
    reg_options = r.json()["options"]
    assert reg_options["rp"]["id"] == "localhost"
    assert reg_options["authenticatorSelection"]["userVerification"] == "required"
    assert reg_options["attestation"] == "none"
    # A registration made for another origin is refused, and the challenge is then spent.
    bad = first.register(reg_options, origin="http://localhost:9999")
    r = client.post(
        "/api/vault/passkeys/register/verify",
        json={"credential": bad, "grant": grant},
        headers=unsafe_headers(csrf),
    )
    assert r.status_code in (400, 403)
    assert options(grant) == 403  # the grant was consumed by that attempt
    enroll(client, ctx, csrf, first)
    assert ctx.passkeys.count() == 1

    # Second passkey: a fresh signature from an existing passkey (less than 2 minutes) is enough.
    token = unlock(client, csrf, first)
    second = SoftAuthenticator(origin=ORIGIN)
    r = client.post("/api/vault/passkeys/register/options", json={}, headers=unsafe_headers(csrf, token))
    assert r.status_code == 200
    r = client.post(
        "/api/vault/passkeys/register/verify",
        json={"credential": second.register(r.json()["options"]), "label": "iPhone"},
        headers=unsafe_headers(csrf, token),
    )
    assert r.status_code == 200
    ctx.clock.advance(3 * 60)  # type: ignore[attr-defined]
    client.get("/api/private/coverage", headers={"X-Vault-Token": token})  # vault still open, but not fresh
    assert options(None, token) == 403
    with ctx.db.connect() as conn:
        kinds = [row[0] for row in conn.execute("SELECT kind FROM audit_log")]
    assert kinds.count("passkey_enrolled") == 2  # every enrollment is recorded (and alerted from phase 3)
    assert "passkey_enrollment_refused" in kinds
    # Revocation from Settings, but never the last passkey.
    token = unlock(client, csrf, first)
    listed = client.get("/api/private/passkeys", headers={"X-Vault-Token": token}).json()
    assert len(listed) == 2
    r = client.post(f"/api/private/passkeys/{listed[1]['id']}/revoke", json={}, headers=unsafe_headers(csrf, token))
    assert r.status_code == 200
    r = client.post(f"/api/private/passkeys/{listed[0]['id']}/revoke", json={}, headers=unsafe_headers(csrf, token))
    assert r.status_code == 400


def test_unconfigured_vault_refuses_enrollment(client: TestClient, ctx: AppContext) -> None:
    csrf = login(client, ctx)
    grant = create_one_time_token(ctx.db, ctx.clock, "enroll")
    r = client.post("/api/vault/passkeys/register/options", json={"grant": grant}, headers=unsafe_headers(csrf))
    assert r.status_code == 409
    assert isinstance(sqlite3.sqlite_version, str)
