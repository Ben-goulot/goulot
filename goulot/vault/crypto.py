"""AES-256-GCM from `cryptography`, nothing home-made (CLAUDE.md, rule 13).

Each ciphertext is bound by its associated data to a stable logical field name, the record's UUID and the
key version, never to a table, a column or a rowid: a moved ciphertext no longer decrypts, while a
migration or a VACUUM breaks nothing (docs/SECURITY.md, vault and encryption).
"""

from __future__ import annotations

import os
import re
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORMAT_V1 = b"\x01"
NONCE_BYTES = 12  # 96 bits, random per record
KEY_BYTES = 32
_FIELD = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


class DecryptionError(Exception):
    """Authentication failed: blocking error, logged without content."""


def associated_data(field: str, record_uuid: str, key_version: int) -> bytes:
    if not _FIELD.match(field):
        raise ValueError("invalid logical field name")
    uuid.UUID(record_uuid)  # raises ValueError if not a UUID
    if key_version < 1:
        raise ValueError("invalid key version")
    return f"goulot:v1:{field}:{record_uuid}:{key_version}".encode("ascii")


def _check_key(key: bytes | bytearray) -> None:
    if len(key) != KEY_BYTES:
        raise ValueError("AES-256 key required")


def encrypt_field(key: bytes | bytearray, field: str, record_uuid: str, key_version: int, plaintext: bytes) -> bytes:
    _check_key(key)
    nonce = os.urandom(NONCE_BYTES)
    ct = AESGCM(bytes(key)).encrypt(nonce, plaintext, associated_data(field, record_uuid, key_version))
    return FORMAT_V1 + nonce + ct


def decrypt_field(key: bytes | bytearray, field: str, record_uuid: str, key_version: int, blob: bytes) -> bytes:
    _check_key(key)
    if len(blob) < 1 + NONCE_BYTES + 16 or blob[:1] != FORMAT_V1:
        raise DecryptionError("malformed ciphertext")
    nonce, ct = blob[1 : 1 + NONCE_BYTES], blob[1 + NONCE_BYTES :]
    try:
        return AESGCM(bytes(key)).decrypt(nonce, ct, associated_data(field, record_uuid, key_version))
    except InvalidTag as exc:
        raise DecryptionError("authentication failed") from exc


def wrap_key(kek: bytes | bytearray, key: bytes | bytearray, label: str) -> bytes:
    """Wrap a key with a key-encryption key (AES-256-GCM, label as associated data)."""
    _check_key(kek)
    nonce = os.urandom(NONCE_BYTES)
    return nonce + AESGCM(bytes(kek)).encrypt(nonce, bytes(key), f"goulot:v1:wrap:{label}".encode("ascii"))


def unwrap_key(kek: bytes | bytearray, wrapped: bytes, label: str) -> bytearray:
    _check_key(kek)
    try:
        raw = AESGCM(bytes(kek)).decrypt(
            wrapped[:NONCE_BYTES], wrapped[NONCE_BYTES:], f"goulot:v1:wrap:{label}".encode("ascii")
        )
    except InvalidTag as exc:
        raise DecryptionError("key unwrap failed") from exc
    return bytearray(raw)


def zeroize(buf: bytearray) -> None:
    """Best effort only: Python may keep other copies (docs/SECURITY.md)."""
    for i in range(len(buf)):
        buf[i] = 0
