"""Stop watching a material, resume it, or delete it (docs/SPEC.md, section 2, « Arrêter le suivi, supprimer »).

Stopping (status `archived`) is reversible: rules off, so no query and no AI call for it; its price series keep
being read while one of its advices is still open, so that the ledger can judge it (silence never counts as a
success). Resuming turns the rules of the validated chain back on, without a new reconstruction.

Deleting is final. Everything that belongs to the material goes, the ledger excepted (CLAUDE.md, rule 12): its past
advices stay on the Reliability screen, marked « matière supprimée ». Public articles stay until their usual purge.
The AI cost records stay too, without the material: the month's spending must not drop. Deleting goes through the
vault: Ma chaîne, signals, quotes, purchases and coverage of the material are erased with it. `secure_delete` is on
for every connection: freed pages are overwritten.
"""

from __future__ import annotations

import sqlite3

from goulot.chain import store
from goulot.db.connection import utcnow
from goulot.ledger import outcomes

# Rows to delete, children first (foreign keys). Fixed statements: nothing assembled from data.
DELETE_STEPS = (
    "DELETE FROM hypotheses WHERE analysis_id IN (SELECT id FROM analyses WHERE material_id = :m)",
    "DELETE FROM analysis_parts WHERE analysis_id IN (SELECT id FROM analyses WHERE material_id = :m)",
    "DELETE FROM analyses WHERE material_id = :m",
    "DELETE FROM event_articles WHERE event_id IN (SELECT id FROM events WHERE material_id = :m)",
    "DELETE FROM event_links WHERE material_id = :m",
    "DELETE FROM events WHERE material_id = :m",
    "DELETE FROM triage WHERE material_id = :m",
    "DELETE FROM verdicts WHERE material_id = :m",
    "DELETE FROM alerts WHERE material_id = :m",
    "DELETE FROM digests WHERE material_id = :m",
    "DELETE FROM case_sources WHERE case_row IN (SELECT id FROM historical_events WHERE material_id = :m)",
    "DELETE FROM historical_events WHERE material_id = :m",
    "DELETE FROM case_reports WHERE material_id = :m",
    "DELETE FROM instrument_links WHERE material_id = :m",
    "DELETE FROM watch_rules WHERE material_id = :m",
    "DELETE FROM quotes WHERE material_id = :m",
    "DELETE FROM jobs WHERE material_id = :m",
    "DELETE FROM user_coverage WHERE material_ref = :m",
    "UPDATE llm_calls SET material_id = NULL WHERE material_id = :m",
    "DELETE FROM materials WHERE id = :m",
)


class LifecycleError(ValueError):
    """French message: it reaches the user."""


def archive(conn: sqlite3.Connection, material_id: str) -> None:
    material = store.get_material(conn, material_id)
    if material is None or material["status"] != "validated":
        raise LifecycleError("seule une matière suivie (chaîne validée) peut être arrêtée")
    store.set_status(conn, material_id, "archived")
    conn.execute("UPDATE watch_rules SET active = 0 WHERE material_id = ?", (material_id,))


def resume(conn: sqlite3.Connection, material_id: str) -> int:
    """Back to watched: the rules of the validated chain again (and its probable route straits). Returns the count."""
    material = store.get_material(conn, material_id)
    if material is None or material["status"] != "archived":
        raise LifecycleError("cette matière n'est pas arrêtée")
    conn.execute("UPDATE materials SET status = 'validated', updated_at = ? WHERE id = ?", (utcnow(), material_id))
    return store.refresh_rules(conn, material_id)


def keeps_prices(conn: sqlite3.Connection, material_id: str) -> bool:
    """An archived material keeps its price series while one of its advices waits for its outcome."""
    return outcomes.open_advice(conn, material_id) > 0


def delete(conn: sqlite3.Connection, material_id: str, typed_name: str) -> None:
    """Public rows of the material; the caller erases its private records in the same request (vault open)."""
    material = store.get_material(conn, material_id)
    if material is None:
        raise LifecycleError("matière introuvable")
    if " ".join(typed_name.split()).casefold() != " ".join(material["name"].split()).casefold():
        raise LifecycleError("le nom tapé ne correspond pas à celui de la matière")
    if material["status"] == "reconstructing":
        raise LifecycleError("une reconstruction est en cours : attends sa fin")
    store.clear_chain(conn, material_id)
    for step in DELETE_STEPS:
        conn.execute(step, {"m": material_id})
