"""What the compliance export keeps: an encrypted profile (mitigation choices, company, signatory) and, for each
export, its fingerprint and an `export` ledger entry; never the content of a report (docs/SPEC.md, section 10).

The profile is one record encrypted with AES-256-GCM, bound to the logical field `compliance_profile.v1` and to its
UUID (CLAUDE.md, rule 13). Mitigation choices are picked by the user from a fixed list: never written by the code.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from goulot.db.connection import Database, utcnow
from goulot.ledger import chain as ledger
from goulot.vault.keys import Vault

FIELD = "compliance_profile.v1"
Choice = Literal["stock_de_securite", "second_fournisseur", "contrat_cadre", "substitution", "autre"]
UUID_PATTERN = r"^[0-9a-f-]{36}$"


class Measures(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choices: list[Choice] = Field(default_factory=list, max_length=5)
    comment: str = Field(default="", max_length=1000)

    @field_validator("choices")
    @classmethod
    def unique(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company: str = Field(default="", max_length=200)
    signatory_name: str = Field(default="", max_length=120)
    signatory_role: str = Field(default="", max_length=120)
    measures: dict[str, Measures] = Field(default_factory=dict, max_length=200)

    @field_validator("measures")
    @classmethod
    def material_ids(cls, v: dict[str, Measures]) -> dict[str, Measures]:
        import re

        for key in v:
            if not re.fullmatch(UUID_PATTERN, key):
                raise ValueError("identifiant de matière invalide")
        return v


def get_profile(db: Database, vault: Vault) -> Profile:
    with db.connect() as conn:
        row = conn.execute("SELECT id, payload, key_version FROM mitigations ORDER BY created_at LIMIT 1").fetchone()
    if row is None:
        return Profile()
    return Profile.model_validate(vault.decrypt_json(FIELD, row["id"], row["key_version"], row["payload"]))


def put_profile(db: Database, vault: Vault, profile: Profile) -> None:
    with db.connect() as conn:
        row = conn.execute("SELECT id FROM mitigations ORDER BY created_at LIMIT 1").fetchone()
    record_id = row["id"] if row else str(uuid.uuid4())
    blob, version = vault.encrypt_json(FIELD, record_id, profile.model_dump(mode="json"))
    now = utcnow()
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO mitigations (id, payload, key_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload = excluded.payload, key_version = excluded.key_version, "
            "updated_at = excluded.updated_at",
            (record_id, blob, version, now, now),
        )


def forget_material(db: Database, vault: Vault, material_id: str) -> None:
    """Deleting a material erases its mitigation choices too."""
    profile = get_profile(db, vault)
    if material_id in profile.measures:
        del profile.measures[material_id]
        put_profile(db, vault, profile)


def verify_all(db: Database, vault: Vault) -> int:
    """`goulot doctor`: the profile still decrypts. Returns the number checked (no content kept)."""
    with db.connect() as conn:
        rows = conn.execute("SELECT id, payload, key_version FROM mitigations").fetchall()
    for r in rows:
        vault.decrypt_json(FIELD, r["id"], r["key_version"], r["payload"])
    return len(rows)


def record(conn: sqlite3.Connection, report: dict[str, Any], at: datetime | None = None) -> int:
    """Inside the caller's transaction: the `export` ledger entry (public content only: template, materials, basis,
    draft flag and fingerprint) and the `compliance_exports` row. A report on Ma chaîne goes in the personal scope,
    so that the market column of the Fiabilité screen never hints at it. Returns the ledger sequence number."""
    materials = [m["id"] for m in report["materials"]]
    content = {"template": report["template"]["id"], "template_version": report["template"]["version"],
               "materials": materials, "basis": report["basis"], "draft": report["draft"],
               "fingerprint": report["fingerprint"],
               "scope": "personal" if report["basis"] == "my_chain" else "market"}
    seq = ledger.append(conn, "export", content, material_id=materials[0] if len(materials) == 1 else None,
                        at=at)
    conn.execute(
        "INSERT INTO compliance_exports (id, created_at, template_id, template_version, basis, material_ids, draft, "
        "fingerprint, ledger_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), utcnow(), report["template"]["id"], report["template"]["version"], report["basis"],
         json.dumps(materials), int(report["draft"]), report["fingerprint"], seq),
    )
    return seq
