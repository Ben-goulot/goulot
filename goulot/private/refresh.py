"""Personal verdicts at each watch cycle (docs/SPEC.md, sections 5 and 11), after the market verdicts.

A material gets a personal verdict when it has a Ma chaîne, supplier signals or a coverage. A new row is written
only when the verdict, the price pressure, X or the action window changes; X, the window, the explanation, the
retained events and the exposures are encrypted together in the row, and its inputs with a keyed digest.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any

from goulot.chain import store
from goulot.chain.delays import delays_to_material
from goulot.db.connection import Database, utcnow
from goulot.ledger import personal as ledger_personal
from goulot.pipeline import analysis
from goulot.pipeline.verdict import LABELS, EventInput
from goulot.prices import store as prices
from goulot.private import coverage, records
from goulot.private.mychain import Model
from goulot.private.personal import Personal, PersonalEvent, compute
from goulot.vault.keys import Vault, VaultError

log = logging.getLogger("goulot.private")

ACTIVE_WINDOW = timedelta(days=60)
VERDICT_FIELD = "verdict_inputs.v1"
PERSONAL_FIELD = "personal_verdict.v1"


def personal_events(conn: sqlite3.Connection, vault: Vault, material_id: str, now: datetime,
                    with_chain: bool) -> list[PersonalEvent]:
    since = (now - ACTIVE_WINDOW).isoformat(timespec="seconds")
    links = records.read_links(conn, vault, material_id) if with_chain else {}
    public = {r[0] for r in conn.execute("SELECT id FROM public_events WHERE material_id = ?", (material_id,))}
    out = []
    for e in conn.execute("SELECT * FROM events WHERE material_id = ? AND last_seen_at >= ? "
                          "AND event_status NOT IN ('resolved')", (material_id, since)).fetchall():
        sites = list(links.get(e["id"], {}).get("sites", []))
        if e["id"] not in public and not sites:
            continue  # found through a former Ma chaîne: it touches nothing any more
        nodes = [r[0] for r in conn.execute("SELECT node_key FROM event_links WHERE event_id = ?", (e["id"],))]
        out.append(PersonalEvent(
            EventInput(id=e["id"], confirmed=e["confirmation"] == "confirmed",
                       best_lineage=e["best_lineage_reliability"], node_keys=nodes,
                       analysis=analysis.latest_valid(conn, e["id"])),
            event_type=e["event_type"], event_status=e["event_status"], title=e["title"], sites=sites,
            public=e["id"] in public))
    return out


def current(conn: sqlite3.Connection, vault: Vault, material_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM verdicts WHERE material_id = ? AND scope = 'personal' AND current = 1",
                       (material_id,)).fetchone()
    if row is None:
        return None
    data: dict[str, Any] = {"id": row["id"], "verdict": row["verdict"], "pressure": row["pressure"],
                            "break_in": row["break_in"], "created_at": row["created_at"],
                            "last_ok_cycle": row["last_ok_cycle"], "rules_only": row["rules_only"]}
    if row["private"] is not None:
        data.update(vault.decrypt_json(PERSONAL_FIELD, row["id"], row["private_key_version"], row["private"]))
    return data


def locked_label(verdict: str) -> str:
    """Label of a personal verdict without X (X with the window would reveal the contractual lead time)."""
    return {"pot": "Problème potentiel"}.get(verdict, LABELS.get(verdict, verdict))


def refresh(db: Database, vault: Vault, now: datetime, models: dict[str, tuple[int, Model]], *, verified: bool,
            last_ok_cycle: str | None, rules_only: bool = False, break_in: bool = True) -> list[tuple[str, Personal]]:
    changed: list[tuple[str, Personal]] = []
    try:
        with db.transaction() as conn:
            covers = {c["material_ref"]: c for c in coverage.list_all(db, vault)}
            signals_all = records.read_all(conn, vault, "signal")
            for m in store.list_materials(conn):
                if m["status"] != "validated":
                    continue
                found = models.get(m["id"])
                signals = [s for s in signals_all if s["material_id"] == m["id"]]
                cover = covers.get(m["id"])
                previous = current(conn, vault, m["id"])
                if found is None and not signals and cover is None:
                    if previous is not None:  # Ma chaîne erased: the personal verdict leaves with it
                        conn.execute("UPDATE verdicts SET current = 0 WHERE material_id = ? AND scope = 'personal'",
                                     (m["id"],))
                    continue
                revision, model = found if found else (None, None)
                p = _compute(conn, vault, m["id"], model, revision, signals, cover, now, verified, rules_only)
                if previous is not None and (previous["verdict"], previous["pressure"], previous.get("x_weeks"),
                                             previous.get("window_weeks")) == (
                        p.verdict.verdict, p.verdict.pressure, p.verdict.x_weeks, p.verdict.window_weeks):
                    continue
                _store(conn, vault, m["id"], p, previous, now, last_ok_cycle, break_in)
                changed.append((m["id"], p))
    except VaultError:
        log.warning("vault key unavailable: personal verdicts not refreshed")
        return []
    return changed


def _compute(conn: sqlite3.Connection, vault: Vault, material_id: str, model: Model | None, revision: int | None,
             signals: list[dict[str, Any]], cover: dict[str, Any] | None, now: datetime, verified: bool,
             rules_only: bool) -> Personal:
    chain = store.load_chain(conn, material_id)
    _, targets, last_prices = prices.market(conn, material_id, None)
    return compute(personal_events(conn, vault, material_id, now, model is not None), signals, model, revision,
                   cover, commodities=set(analysis.commodities(chain["nodes"])), instruments=targets,
                   delays=delays_to_material(chain["nodes"], chain["edges"]), last_prices=last_prices,
                   today=now.date(), verified=verified, rules_only=rules_only)


def _store(conn: sqlite3.Connection, vault: Vault, material_id: str, p: Personal, previous: dict[str, Any] | None,
           now: datetime, last_ok_cycle: str | None, break_in: bool) -> None:
    v = p.verdict
    verdict_id = str(uuid.uuid4())
    inputs = {"material": material_id, "revision": p.revision, "exposures": p.exposures,
              "signals": p.signals_used, "threshold_pct": p.threshold_pct, "at": now.isoformat(timespec="seconds")}
    blob, version = vault.encrypt_json(VERDICT_FIELD, verdict_id, inputs)
    digest = vault.inputs_digest(inputs)
    private, private_version = vault.encrypt_json(PERSONAL_FIELD, verdict_id, {
        "label": v.label, "x_weeks": v.x_weeks, "x_low": v.x_low, "x_high": v.x_high,
        "window_weeks": v.window_weeks, "window_assumed": v.window_assumed, "explanation": v.explanation,
        "event_ids": v.event_ids, "exposures": p.exposures, "plan_b": p.plan_b, "threshold_pct": p.threshold_pct,
        "revision": p.revision,
        "retained": [{"id": r.id, "rule": r.rule, "exposure_pct": round(100 * r.exposure, 1), "s": r.s,
                      "s_perso": r.s_perso, "x_weeks": round(r.x, 1), "sites": r.sites} for r in p.retained],
    })
    ledger_personal.record_verdict(conn, vault, material_id, p, inputs_digest=digest,
                                   previous_pressure=previous["pressure"] if previous else None, at=now)
    conn.execute("UPDATE verdicts SET current = 0 WHERE material_id = ? AND scope = 'personal'", (material_id,))
    conn.execute(
        "INSERT INTO verdicts (id, material_id, scope, verdict, pressure, x_weeks, x_low, x_high, window_weeks, "
        "window_assumed, rules_only, break_in, explanation, event_ids, inputs, inputs_key_version, inputs_digest, "
        "last_ok_cycle, created_at, private, private_key_version) VALUES (?, ?, 'personal', ?, ?, NULL, NULL, NULL, "
        "NULL, 1, ?, ?, '', '[]', ?, ?, ?, ?, ?, ?, ?)",
        (verdict_id, material_id, v.verdict, v.pressure, int(v.rules_only), int(break_in), blob, version, digest,
         last_ok_cycle, utcnow(), private, private_version),
    )

