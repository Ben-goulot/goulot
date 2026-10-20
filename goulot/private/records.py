"""Encrypted private records (docs/SPEC.md, section 11): Ma chaîne revisions, supplier signals, quotes, purchases and
private links. Each payload carries its material inside the ciphertext, bound to a logical field and to the record
UUID; reading means decrypting every record of a kind, which stays small (a few hundred at most).

Nothing here logs a payload, and nothing is ever written in clear (CLAUDE.md, rule 13).
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any

from goulot.db.connection import Database, utcnow
from goulot.vault.keys import Vault

KINDS: dict[str, tuple[str, str]] = {
    # kind: (table, logical field bound into the ciphertext)
    "chain": ("private_chain", "my_chain.v1"),
    "signal": ("user_signals", "user_signal.v1"),
    "quote": ("user_quotes", "user_quote.v1"),
    "purchase": ("user_purchases", "user_purchase.v1"),
}
# Statements written out per table: no table name is ever assembled into SQL.
_INSERT = {
    "private_chain": "INSERT INTO private_chain (id, payload, key_version, created_at) VALUES (?, ?, ?, ?)",
    "user_signals": "INSERT INTO user_signals (id, payload, key_version, created_at) VALUES (?, ?, ?, ?)",
    "user_quotes": "INSERT INTO user_quotes (id, payload, key_version, created_at) VALUES (?, ?, ?, ?)",
    "user_purchases": "INSERT INTO user_purchases (id, payload, key_version, created_at) VALUES (?, ?, ?, ?)",
}
_SELECT = {
    "private_chain": "SELECT id, payload, key_version, created_at FROM private_chain ORDER BY created_at, rowid",
    "user_signals": "SELECT id, payload, key_version, created_at FROM user_signals ORDER BY created_at, rowid",
    "user_quotes": "SELECT id, payload, key_version, created_at FROM user_quotes ORDER BY created_at, rowid",
    "user_purchases": "SELECT id, payload, key_version, created_at FROM user_purchases ORDER BY created_at, rowid",
}
_DELETE = {
    "private_chain": "DELETE FROM private_chain WHERE id = ?",
    "user_signals": "DELETE FROM user_signals WHERE id = ?",
    "user_quotes": "DELETE FROM user_quotes WHERE id = ?",
    "user_purchases": "DELETE FROM user_purchases WHERE id = ?",
}
LINKS_FIELD = "private_links.v1"


def add(db: Database, vault: Vault, kind: str, payload: dict[str, Any]) -> str:
    table, field = KINDS[kind]
    record_id = str(uuid.uuid4())
    blob, version = vault.encrypt_json(field, record_id, payload)
    with db.transaction() as conn:
        conn.execute(_INSERT[table], (record_id, blob, version, utcnow()))
    return record_id


def read_all(conn: sqlite3.Connection, vault: Vault, kind: str, material_id: str | None = None
             ) -> list[dict[str, Any]]:
    """Decrypted records of a kind, oldest first, each with its `id` and `created_at`."""
    table, field = KINDS[kind]
    rows = conn.execute(_SELECT[table]).fetchall()
    out = []
    for r in rows:
        data = vault.decrypt_json(field, r["id"], r["key_version"], r["payload"])
        if material_id is None or data.get("material_id") == material_id:
            out.append({**data, "id": r["id"], "created_at": r["created_at"]})
    return out


def delete(db: Database, vault: Vault, kind: str, record_id: str, material_id: str) -> bool:
    """Delete one record of a material (quotes, purchases, signals). `secure_delete` overwrites the freed pages."""
    table, _ = KINDS[kind]
    with db.transaction() as conn:
        mine = {r["id"] for r in read_all(conn, vault, kind, material_id)}
        if record_id not in mine:
            return False
        return conn.execute(_DELETE[table], (record_id,)).rowcount == 1


def erase_material(db: Database, vault: Vault, material_id: str) -> int:
    """« Effacer Ma chaîne » of a material: every revision, signal, quote, purchase and link of it."""
    removed = 0
    with db.transaction() as conn:
        for kind, (table, _) in KINDS.items():
            for r in read_all(conn, vault, kind, material_id):
                removed += conn.execute(_DELETE[table], (r["id"],)).rowcount
        row = _links_row(conn, vault, material_id)
        if row is not None:
            removed += conn.execute("DELETE FROM private_links WHERE id = ?", (row[0],)).rowcount
    return removed


def _links_row(conn: sqlite3.Connection, vault: Vault, material_id: str) -> tuple[str, dict[str, Any]] | None:
    for r in conn.execute("SELECT id, payload, key_version FROM private_links").fetchall():
        data = vault.decrypt_json(LINKS_FIELD, r["id"], r["key_version"], r["payload"])
        if data.get("material_id") == material_id:
            return r["id"], data
    return None


def read_links(conn: sqlite3.Connection, vault: Vault, material_id: str) -> dict[str, Any]:
    """event_id -> {"sites": [site ids], "how": [...]}; empty without Ma chaîne."""
    row = _links_row(conn, vault, material_id)
    links: dict[str, Any] = row[1]["links"] if row else {}
    return links


def write_links(conn: sqlite3.Connection, vault: Vault, material_id: str, links: dict[str, Any]) -> None:
    row = _links_row(conn, vault, material_id)
    record_id = row[0] if row else str(uuid.uuid4())
    blob, version = vault.encrypt_json(LINKS_FIELD, record_id, {"material_id": material_id, "links": links})
    conn.execute("INSERT INTO private_links (id, payload, key_version, updated_at) VALUES (?, ?, ?, ?) "
                 "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, key_version = excluded.key_version, "
                 "updated_at = excluded.updated_at", (record_id, blob, version, utcnow()))


def verify_all(db: Database, vault: Vault) -> int:
    """`goulot doctor`: every private record still decrypts. Returns the number checked (no content kept)."""
    count = 0
    with db.connect() as conn:
        for kind in KINDS:
            count += len(read_all(conn, vault, kind))
        count += int(conn.execute("SELECT COUNT(*) FROM private_links").fetchone()[0])
        for r in conn.execute("SELECT id, payload, key_version FROM private_links").fetchall():
            vault.decrypt_json(LINKS_FIELD, r["id"], r["key_version"], r["payload"])
    return count
