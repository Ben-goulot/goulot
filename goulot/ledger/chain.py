"""Append-only advice ledger chained by SHA-256 (CLAUDE.md, rule 12; docs/SPEC.md, section 7).

hash = SHA-256(prev_hash ‖ seq ‖ at ‖ material ‖ kind ‖ canonical content ‖ SHA-256 of the private ciphertext).
The chain is checked without any key; private fields are encrypted apart and never re-encrypted. A correction is a
new entry pointing to the old one. `verify` reports the first broken link: a modified, deleted or inserted entry.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from goulot.vault.keys import Vault

GENESIS = "0" * 64
KINDS = ("verdict", "hypothesis", "outcome", "user_feedback", "signal", "pressure", "missed_move",
         "settings_change", "export", "correction")
PRIVATE_FIELD = "ledger_private.v1"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def private_record_id(seq: int) -> str:
    """Stable UUID of an entry's private fields, bound into the ciphertext (associated data)."""
    return str(uuid.uuid5(uuid.NAMESPACE_OID, f"goulot.ledger.{seq}"))


def entry_hash(prev_hash: str, seq: int, at: str, material_id: str | None, kind: str, content: str,
               private_digest: str) -> str:
    payload = "\x1f".join([prev_hash, str(seq), at, material_id or "", kind, content, private_digest])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def append(conn: sqlite3.Connection, kind: str, content: dict[str, Any], *, material_id: str | None = None,
           private: dict[str, Any] | None = None, vault: Vault | None = None, at: datetime | None = None) -> int:
    """Add one entry inside the caller's transaction; returns its sequence number."""
    if kind not in KINDS:
        raise ValueError(f"unknown ledger kind {kind}")
    last = conn.execute("SELECT seq, hash FROM ledger ORDER BY seq DESC LIMIT 1").fetchone()
    seq = (last[0] + 1) if last else 1
    prev = last[1] if last else GENESIS
    stamp = (at or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds")
    body = canonical(content)
    blob, version, digest = None, None, ""
    if private:
        if vault is None:
            raise ValueError("private ledger fields need the vault")
        blob, version = vault.encrypt_json(PRIVATE_FIELD, private_record_id(seq), private)
        digest = hashlib.sha256(blob).hexdigest()
    h = entry_hash(prev, seq, stamp, material_id, kind, body, digest)
    conn.execute(
        "INSERT INTO ledger (seq, at, material_id, kind, content, private_blob, private_key_version, private_digest, "
        "prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (seq, stamp, material_id, kind, body, blob, version, digest, prev, h),
    )
    return seq


@dataclass
class Verification:
    ok: bool
    entries: int
    head: str
    problem: str = ""
    at_seq: int | None = None


def verify(conn: sqlite3.Connection) -> Verification:
    prev, expected_seq, count = GENESIS, 1, 0
    for r in conn.execute("SELECT * FROM ledger ORDER BY seq"):
        count += 1
        if r["seq"] != expected_seq:
            return Verification(False, count, prev, "entrée manquante (suppression)", expected_seq)
        if r["prev_hash"] != prev:
            return Verification(False, count, prev, "chaînage rompu", r["seq"])
        digest = hashlib.sha256(r["private_blob"]).hexdigest() if r["private_blob"] is not None else ""
        if digest != r["private_digest"]:
            return Verification(False, count, prev, "champs privés modifiés", r["seq"])
        if entry_hash(prev, r["seq"], r["at"], r["material_id"], r["kind"], r["content"], digest) != r["hash"]:
            return Verification(False, count, prev, "entrée modifiée", r["seq"])
        prev, expected_seq = r["hash"], expected_seq + 1
    return Verification(True, count, prev)


def entries(conn: sqlite3.Connection, *, material_id: str | None = None, kinds: tuple[str, ...] = KINDS,
            scope: str | None = "market") -> list[dict[str, Any]]:
    """Public content of the entries. Entries of the personal scope (Ma chaîne, signals, feedback, personal
    verdicts and their outcomes) are left out unless `scope` is "personal" or None: a caller that does not ask for
    them never sees them, even by mistake."""
    rows = conn.execute("SELECT seq, at, material_id, kind, content FROM ledger ORDER BY seq").fetchall()
    out = []
    for r in rows:
        if r["kind"] not in kinds or (material_id is not None and r["material_id"] != material_id):
            continue
        content = json.loads(r["content"])
        if scope is not None and content.get("scope", "market") != scope:
            continue
        out.append({"seq": r["seq"], "at": r["at"], "material_id": r["material_id"], "kind": r["kind"], **content})
    return out


def private_fields(conn: sqlite3.Connection, vault: Vault, seq: int) -> dict[str, Any]:
    """Decrypted private fields of one entry ({} when it has none)."""
    row = conn.execute("SELECT private_blob, private_key_version FROM ledger WHERE seq = ?", (seq,)).fetchone()
    if row is None or row["private_blob"] is None:
        return {}
    data: dict[str, Any] = vault.decrypt_json(PRIVATE_FIELD, private_record_id(seq), row["private_key_version"],
                                              row["private_blob"])
    return data
