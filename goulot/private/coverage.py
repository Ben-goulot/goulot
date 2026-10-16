"""Coverage per material (stock, supplier lead time, price threshold): private, encrypted, never sent to the AI.

The ciphertext is bound to the logical field `coverage.v1` and to the record UUID (docs/SECURITY.md).
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from goulot.db.connection import Database, utcnow
from goulot.vault.keys import Vault

FIELD = "coverage.v1"
MATERIAL_REF_PATTERN = r"^[A-Za-z0-9._-]{1,64}$"


class Coverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stock_weeks: float | None = Field(default=None, ge=0, le=520)
    supplier_lead_time_days: int | None = Field(default=None, ge=0, le=730)
    price_threshold_pct: float | None = Field(default=None, ge=0, le=100)


def put(db: Database, vault: Vault, material_ref: str, coverage: Coverage) -> None:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM user_coverage WHERE material_ref = ?", (material_ref,)).fetchone()
    record_id = row["id"] if row else str(uuid.uuid4())
    blob, version = vault.encrypt_json(FIELD, record_id, coverage.model_dump())
    now = utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO user_coverage (id, material_ref, payload, key_version, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, "
            "key_version = excluded.key_version, updated_at = excluded.updated_at",
            (record_id, material_ref, blob, version, now, now),
        )


def list_all(db: Database, vault: Vault) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute("SELECT id, material_ref, payload, key_version FROM user_coverage").fetchall()
    return [
        {"material_ref": r["material_ref"], **vault.decrypt_json(FIELD, r["id"], r["key_version"], r["payload"])}
        for r in rows
    ]


def delete(db: Database, material_ref: str) -> bool:
    with db.transaction() as conn:
        return conn.execute("DELETE FROM user_coverage WHERE material_ref = ?", (material_ref,)).rowcount == 1


def verify_all(db: Database, vault: Vault) -> int:
    """`goulot doctor`: every encrypted field still decrypts. Returns the number checked (no content kept)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, payload, key_version FROM user_coverage").fetchall()
    for r in rows:
        vault.decrypt_json(FIELD, r["id"], r["key_version"], r["payload"])
    return len(rows)
