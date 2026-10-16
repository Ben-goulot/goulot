"""Key hierarchy (docs/SECURITY.md, vault and encryption).

- Master key, 256 bits, in the keychain item `goulot-vault` (separate from the `goulot` service).
- Versioned data keys and the ledger HMAC key, wrapped by the master key, in `vault_keys`.
- A copy of the master key wrapped by a key derived (HKDF-SHA-256) from the recovery key.
The master key is never written to the database or to a backup. A new data key never re-encrypts anything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import keyring
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from goulot.db.connection import Database, get_setting, set_setting, utcnow
from goulot.security.audit import audit
from goulot.vault import recovery
from goulot.vault.crypto import (
    KEY_BYTES,
    DecryptionError,
    decrypt_field,
    encrypt_field,
    unwrap_key,
    wrap_key,
    zeroize,
)

MASTER_SERVICE = "goulot-vault"
MASTER_ACCOUNT = "master-key"
_SALT_BYTES = 16


class VaultError(Exception):
    pass


class VaultNotConfigured(VaultError):
    pass


class MasterKeyUnavailable(VaultError):
    pass


def _recovery_kek(raw: bytes, salt: bytes) -> bytearray:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=KEY_BYTES, salt=salt, info=b"goulot:v1:recovery")
    return bytearray(hkdf.derive(raw))


def _wrap_master_for_recovery(master: bytearray, raw_recovery: bytes) -> bytes:
    salt = os.urandom(_SALT_BYTES)
    kek = _recovery_kek(raw_recovery, salt)
    try:
        return salt + wrap_key(kek, master, "master_recovery")
    finally:
        zeroize(kek)


def _unwrap_master_with_recovery(blob: bytes, raw_recovery: bytes) -> bytearray:
    kek = _recovery_kek(raw_recovery, blob[:_SALT_BYTES])
    try:
        return unwrap_key(kek, blob[_SALT_BYTES:], "master_recovery")
    finally:
        zeroize(kek)


class Vault:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- state -----------------------------------------------------------------------------------------
    def is_configured(self) -> bool:
        with self.db.connect() as conn:
            return conn.execute("SELECT 1 FROM vault_keys WHERE kind = 'master_recovery'").fetchone() is not None

    def recovery_confirmed(self) -> bool:
        with self.db.connect() as conn:
            return get_setting(conn, "vault_recovery_confirmed") == "1"

    def mark_recovery_confirmed(self) -> None:
        with self.db.transaction() as conn:
            set_setting(conn, "vault_recovery_confirmed", "1")
            audit(conn, "vault_recovery_key_confirmed")

    # --- master key ------------------------------------------------------------------------------------
    @staticmethod
    def _store_master(master: bytearray) -> None:
        keyring.set_password(MASTER_SERVICE, MASTER_ACCOUNT, base64.b64encode(bytes(master)).decode("ascii"))

    @staticmethod
    def master_available() -> bool:
        return bool(keyring.get_password(MASTER_SERVICE, MASTER_ACCOUNT))

    @staticmethod
    def _load_master() -> bytearray:
        value = keyring.get_password(MASTER_SERVICE, MASTER_ACCOUNT)
        if not value:
            raise MasterKeyUnavailable("Clé maîtresse absente du trousseau.")
        raw = bytearray(base64.b64decode(value))
        if len(raw) != KEY_BYTES:
            raise MasterKeyUnavailable("Clé maîtresse illisible.")
        return raw

    @contextmanager
    def _master(self) -> Iterator[bytearray]:
        master = self._load_master()
        try:
            yield master
        finally:
            zeroize(master)

    # --- creation, recovery --------------------------------------------------------------------------
    def create(self) -> str:
        """Create the vault. Returns the recovery key, to be shown once in the Terminal."""
        if self.is_configured():
            raise VaultError("Le coffre existe déjà.")
        if self.master_available():
            raise VaultError("Une clé maîtresse existe déjà dans le trousseau : utilisez « goulot vault recover ».")
        master = bytearray(os.urandom(KEY_BYTES))
        data_key = bytearray(os.urandom(KEY_BYTES))
        ledger_key = bytearray(os.urandom(KEY_BYTES))
        raw_recovery = recovery.generate()
        try:
            with self.db.transaction() as conn:
                now = utcnow()
                conn.execute(
                    "INSERT INTO vault_keys (kind, version, wrapped, created_at) VALUES ('data', 1, ?, ?)",
                    (wrap_key(master, data_key, "data:1"), now),
                )
                conn.execute(
                    "INSERT INTO vault_keys (kind, version, wrapped, created_at) VALUES ('ledger_hmac', 1, ?, ?)",
                    (wrap_key(master, ledger_key, "ledger_hmac:1"), now),
                )
                conn.execute(
                    "INSERT INTO vault_keys (kind, version, wrapped, created_at) VALUES ('master_recovery', 1, ?, ?)",
                    (_wrap_master_for_recovery(master, raw_recovery), now),
                )
                set_setting(conn, "vault_recovery_confirmed", "0")
                set_setting(conn, "vault_user_id", os.urandom(16).hex())
                audit(conn, "vault_created")
                self._store_master(master)
            return recovery.encode(raw_recovery)
        finally:
            zeroize(master)
            zeroize(data_key)
            zeroize(ledger_key)

    def _recovery_blob(self, conn: sqlite3.Connection) -> tuple[int, bytes]:
        row = conn.execute(
            "SELECT version, wrapped FROM vault_keys WHERE kind = 'master_recovery' ORDER BY version DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise VaultNotConfigured("Coffre non configuré.")
        return row["version"], row["wrapped"]

    def check_recovery_key(self, text: str) -> bool:
        """True if the recovery key opens the stored copy of the master key."""
        raw = recovery.decode(text)
        with self.db.connect() as conn:
            _, blob = self._recovery_blob(conn)
        try:
            master = _unwrap_master_with_recovery(blob, raw)
        except DecryptionError:
            return False
        zeroize(master)
        return True

    def recover(self, text: str, *, replace_existing: bool = False) -> None:
        """`goulot vault recover`: restore the master key into the keychain from the recovery key."""
        raw = recovery.decode(text)
        with self.db.connect() as conn:
            _, blob = self._recovery_blob(conn)
        try:
            master = _unwrap_master_with_recovery(blob, raw)
        except DecryptionError as exc:
            raise VaultError("Clé de secours refusée pour cette base.") from exc
        try:
            if self.master_available() and not replace_existing:
                with self._master() as current:
                    if hmac.compare_digest(bytes(current), bytes(master)):
                        return
                raise VaultError("Une autre clé maîtresse est déjà dans le trousseau.")
            self._store_master(master)
            with self.db.transaction() as conn:
                audit(conn, "vault_recovered")
        finally:
            zeroize(master)

    def new_recovery_key(self) -> str:
        """Issue a new recovery key and invalidate the previous one. The caller proves the vault is open."""
        raw_recovery = recovery.generate()
        with self._master() as master, self.db.transaction() as conn:
            version, _ = self._recovery_blob(conn)
            conn.execute(
                "INSERT INTO vault_keys (kind, version, wrapped, created_at) VALUES ('master_recovery', ?, ?, ?)",
                (version + 1, _wrap_master_for_recovery(master, raw_recovery), utcnow()),
            )
            conn.execute("DELETE FROM vault_keys WHERE kind = 'master_recovery' AND version <= ?", (version,))
            set_setting(conn, "vault_recovery_confirmed", "0")
            audit(conn, "vault_recovery_key_renewed")
        return recovery.encode(raw_recovery)

    # --- data keys -------------------------------------------------------------------------------------
    def _unwrap(self, conn: sqlite3.Connection, kind: str, version: int | None) -> tuple[int, bytearray]:
        if version is None:
            row = conn.execute(
                "SELECT version, wrapped FROM vault_keys WHERE kind = ? ORDER BY version DESC LIMIT 1", (kind,)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT version, wrapped FROM vault_keys WHERE kind = ? AND version = ?", (kind, version)
            ).fetchone()
        if row is None:
            raise VaultNotConfigured("Coffre non configuré.")
        with self._master() as master:
            return row["version"], unwrap_key(master, row["wrapped"], f"{kind}:{row['version']}")

    def encrypt_json(self, field: str, record_uuid: str, obj: Any) -> tuple[bytes, int]:
        plaintext = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self.db.connect() as conn:
            version, key = self._unwrap(conn, "data", None)
        try:
            return encrypt_field(key, field, record_uuid, version, plaintext), version
        finally:
            zeroize(key)

    def decrypt_json(self, field: str, record_uuid: str, key_version: int, blob: bytes) -> Any:
        with self.db.connect() as conn:
            _, key = self._unwrap(conn, "data", key_version)
        try:
            return json.loads(decrypt_field(key, field, record_uuid, key_version, blob))
        finally:
            zeroize(key)

    def inputs_digest(self, inputs: dict[str, Any]) -> str:
        """Keyed digest (HMAC-SHA-256) of a verdict's inputs: a plain hash would let anyone try every
        plausible stock, lead time and threshold (docs/SECURITY.md, threat 29)."""
        canonical = json.dumps(inputs, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self.db.connect() as conn:
            _, key = self._unwrap(conn, "ledger_hmac", None)
        try:
            return hmac.new(bytes(key), canonical, hashlib.sha256).hexdigest()
        finally:
            zeroize(key)
