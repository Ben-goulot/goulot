"""Ma chaîne revisions: save, load, and the typical chain it overlays (docs/SPEC.md, section 11).

The background watch reads the master key from the keychain (standard mode): when the key cannot be read (vault
absent, keychain access to approve again after an update), every private step is skipped and the watch goes on with
the market verdict alone.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from goulot.chain import store
from goulot.chain.delays import delays_to_material
from goulot.db.connection import Database
from goulot.private import records
from goulot.private.mychain import Check, Model, MyChain, check
from goulot.vault.keys import Vault, VaultError

log = logging.getLogger("goulot.private")


class NotReady(Exception):
    """The first entry needs a configured vault: recovery key confirmed and a passkey enrolled."""


def usable(vault: Vault) -> bool:
    """True when the background watch may compute private things (standard mode)."""
    try:
        return vault.is_configured() and vault.master_available()
    except Exception:  # keychain refused or locked: the market verdict goes on alone
        log.warning("vault key unavailable: personal steps skipped")
        return False


def typical(conn: sqlite3.Connection, material_id: str) -> dict[str, Any]:
    chain = store.load_chain(conn, material_id)
    return {"nodes": chain["nodes"], "edges": chain["edges"],
            "delays": delays_to_material(chain["nodes"], chain["edges"])}


def latest_all(conn: sqlite3.Connection, vault: Vault) -> dict[str, tuple[int, MyChain]]:
    out: dict[str, tuple[int, MyChain]] = {}
    for r in records.read_all(conn, vault, "chain"):
        mid = r["material_id"]
        if mid not in out or r["revision"] > out[mid][0]:
            out[mid] = (int(r["revision"]), MyChain.model_validate(r["chain"]))
    return out


def latest(conn: sqlite3.Connection, vault: Vault, material_id: str) -> tuple[int, MyChain] | None:
    return latest_all(conn, vault).get(material_id)


def revisions(conn: sqlite3.Connection, vault: Vault, material_id: str) -> list[dict[str, Any]]:
    return [{"revision": r["revision"], "created_at": r["created_at"]}
            for r in records.read_all(conn, vault, "chain", material_id)]


def save(db: Database, vault: Vault, material_id: str, doc: MyChain) -> tuple[int, Check]:
    """Append a revision once the checks pass. Returns (revision, check); revision 0 when refused."""
    if not (vault.is_configured() and vault.recovery_confirmed()):
        raise NotReady("Coffre non configuré : clé de secours à confirmer (« ./bin/goulot setup »).")
    with db.connect() as conn:
        if store.get_material(conn, material_id) is None:
            raise KeyError(material_id)
        result = check(doc, typical(conn, material_id))
        current = latest(conn, vault, material_id)
    if not result.ok:
        return 0, result
    revision = (current[0] if current else 0) + 1
    records.add(db, vault, "chain", {"material_id": material_id, "revision": revision,
                                     "chain": doc.model_dump(mode="json")})
    return revision, result


def model(conn: sqlite3.Connection, vault: Vault, material_id: str) -> tuple[int, Model] | None:
    found = latest(conn, vault, material_id)
    if found is None:
        return None
    return found[0], Model(found[1], typical(conn, material_id))


def safe_models(conn: sqlite3.Connection, vault: Vault) -> dict[str, tuple[int, Model]]:
    """Every material's Ma chaîne as a model, or nothing when the key cannot be read."""
    if not usable(vault):
        return {}
    try:
        return {mid: (rev, Model(doc, typical(conn, mid))) for mid, (rev, doc) in latest_all(conn, vault).items()}
    except VaultError:
        log.warning("vault key unavailable: personal steps skipped")
        return {}
