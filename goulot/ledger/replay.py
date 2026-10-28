"""« Rejouer cette journée » (docs/SPEC.md, section 7): the market verdict recomputed by the code from the data dated
before the advice (events seen, analyses done, chain in force, last prices), then compared with the one in the
ledger. Read only, no AI call. With the vault open, the inputs kept with the verdict are also checked against the
keyed digest written in the ledger that day.

Limit, said on screen: an event's confirmation and sources keep changing after the day; the replay takes their
current state. A different result is shown as such, never smoothed over.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from typing import Any

from goulot.chain.delays import delays_to_material
from goulot.ledger import chain
from goulot.ledger.report import VERDICT_WORDS
from goulot.pipeline import analysis
from goulot.pipeline.assess import ACTIVE_WINDOW, VERDICT_FIELD
from goulot.pipeline.verdict import EventInput, Verdict, compute
from goulot.prices import store as prices
from goulot.vault.crypto import DecryptionError
from goulot.vault.keys import Vault

CAVEAT = ("Recalcul à partir des données datées d'avant ce jour. La confirmation et les sources d'un événement ont "
          "pu évoluer depuis : leur état actuel est utilisé.")


class ReplayError(LookupError):
    """French message: it reaches the user."""


def verdict_entry(conn: sqlite3.Connection, seq: int) -> dict[str, Any]:
    entry = next((e for e in chain.entries(conn, kinds=("verdict",)) if e["seq"] == seq), None)
    if entry is None:
        raise ReplayError("seul un verdict du marché se rejoue")
    return entry


def _chain_at(conn: sqlite3.Connection, material_id: str, at: str) -> tuple[list[dict[str, Any]],
                                                                          list[dict[str, Any]]]:
    """Rows of the bitemporal chain in force at `at`."""
    nodes = [dict(r) for r in conn.execute(
        "SELECT * FROM chain_nodes WHERE material_id = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
        (material_id, at, at))]
    edges = [dict(r) for r in conn.execute(
        "SELECT * FROM chain_edges WHERE material_id = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
        (material_id, at, at))]
    return nodes, edges


def _analysis_at(conn: sqlite3.Connection, event_id: str, at: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT result FROM analyses WHERE event_id = ? AND state = 'done' AND done_at <= ? "
                       "ORDER BY done_at DESC LIMIT 1", (event_id, at)).fetchone()
    return json.loads(row[0]) if row else None


def _events_at(conn: sqlite3.Connection, material_id: str, at: datetime,
               nodes: list[dict[str, Any]]) -> list[EventInput]:
    stamp = at.isoformat(timespec="seconds")
    shares = {n["node_key"]: n["global_share_pct"] for n in nodes}
    out = []
    for e in conn.execute(
        "SELECT * FROM public_events WHERE material_id = ? AND first_seen_at <= ? AND last_seen_at >= ? "
        "AND NOT (event_status = 'resolved' AND updated_at <= ?) ORDER BY first_seen_at",
        (material_id, stamp, (at - ACTIVE_WINDOW).isoformat(timespec="seconds"), stamp),
    ):
        keys = [r[0] for r in conn.execute("SELECT node_key FROM event_links WHERE event_id = ?", (e["id"],))]
        known = [shares[k] for k in keys if shares.get(k) is not None]
        out.append(EventInput(id=e["id"], confirmed=e["confirmation"] == "confirmed",
                              best_lineage=e["best_lineage_reliability"], node_keys=keys,
                              analysis=_analysis_at(conn, e["id"], stamp),
                              exposed_share_pct=max(known) if known else None))
    return out


def _prices_at(conn: sqlite3.Connection, material_id: str, day: date) -> tuple[set[str], dict[str, float]]:
    targets: set[str] = set()
    last: dict[str, float] = {}
    for r in prices.links(conn, material_id):
        s = prices.series(conn, r["instrument_id"])
        point = s.last_before(day) if s is not None else None
        if point is None:
            continue
        last[r["instrument_id"]] = point[1]
        if r["role"] != "factor":
            targets.add(r["instrument_id"])
    proxy = prices.proxy_index(conn, material_id)
    point = proxy.last_before(day) if proxy is not None else None
    if point is not None:
        pid = f"proxy:{material_id[:8]}"
        targets.add(pid)
        last[pid] = point[1]
    return targets, last


def _words(verdict: str, x_weeks: float | None, pressure: int) -> str:
    label = VERDICT_WORDS.get(verdict, verdict)
    if verdict == "pot" and x_weeks is not None:
        label += " à " + f"{x_weeks:g}".replace(".", ",") + " sem."
    return label + (f" (pression {pressure:+d})" if pressure else "")


def replay(conn: sqlite3.Connection, seq: int) -> dict[str, Any]:
    entry = verdict_entry(conn, seq)
    material_id = entry["material_id"]
    if conn.execute("SELECT 1 FROM materials WHERE id = ?", (material_id,)).fetchone() is None:
        raise ReplayError("matière supprimée : les données de ce jour-là sont effacées, seul le registre reste")
    at = datetime.fromisoformat(entry["at"])
    nodes, edges = _chain_at(conn, material_id, entry["at"])
    events = _events_at(conn, material_id, at, nodes)
    targets, last = _prices_at(conn, material_id, at.date())
    v: Verdict = compute(events, commodities=set(analysis.commodities(nodes)), instruments=targets,
                         delays=delays_to_material(nodes, edges), last_prices=last,
                         verified=entry["verdict"] != "unverified", rules_only=bool(entry.get("rules_only")))
    recorded = (entry["verdict"], entry.get("x_weeks"), entry.get("pressure") or 0)
    differences = []
    if v.verdict != recorded[0]:
        differences.append("verdict")
    if v.x_weeks != recorded[1]:
        differences.append("délai X")
    if v.pressure != recorded[2]:
        differences.append("pression de prix")
    if sorted(v.event_ids) != sorted(entry.get("event_ids") or []):
        differences.append("événements retenus")
    return {
        "seq": seq, "same": not differences, "differences": differences,
        "recorded": _words(*recorded), "replayed": _words(v.verdict, v.x_weeks, v.pressure),
        "events": len(events), "chain_nodes": len(nodes), "explanation": v.explanation, "caveat": CAVEAT,
    }


def inputs_check(conn: sqlite3.Connection, vault: Vault, seq: int) -> dict[str, Any]:
    """The inputs kept (encrypted) with the verdict hash to the keyed digest the ledger holds. Says nothing else."""
    entry = verdict_entry(conn, seq)
    digest = entry.get("inputs_digest") or ""
    if not digest:
        return {"status": "absent", "message": "empreinte des données non gardée ce jour-là (coffre indisponible)"}
    row = conn.execute("SELECT id, inputs, inputs_key_version FROM verdicts WHERE material_id = ? AND scope = 'market' "
                       "AND inputs_digest = ?", (entry["material_id"], digest)).fetchone()
    if row is None or row["inputs"] is None:
        return {"status": "gone", "message": "données du calcul introuvables : effacées ou modifiées depuis"}
    try:
        inputs = vault.decrypt_json(VERDICT_FIELD, row["id"], row["inputs_key_version"], row["inputs"])
    except DecryptionError:  # authenticated encryption: a changed byte fails here
        return {"status": "altered", "message": "données du calcul modifiées : leur déchiffrement échoue"}
    if vault.inputs_digest(inputs) == digest:
        return {"status": "intact", "message": "données du calcul intactes : leur empreinte correspond au registre"}
    return {"status": "altered", "message": "données du calcul modifiées : leur empreinte ne correspond plus"}
