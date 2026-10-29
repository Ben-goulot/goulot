"""Analyses and verdicts at each watch cycle (docs/SPEC.md, sections 4 and 5).

- Events that meet the trigger, never analyzed or changed since (confirmation, lineages, severity hint,
  status), are analyzed: right away on the standard API when the severity hint is 3 or more, otherwise in a
  Batch collected at a later cycle.
- Then the market verdict of every validated material is recomputed; a new verdict row is written only when the
  verdict, the price pressure or X changes. Its inputs are stored encrypted with an HMAC digest.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from goulot.chain import store
from goulot.chain.delays import delays_to_material
from goulot.db.connection import Database, utcnow
from goulot.history import similarity
from goulot.ledger import outcomes
from goulot.ledger import personal as ledger_personal
from goulot.llm import batch
from goulot.llm.backend import MessageBackend
from goulot.llm.batch import BatchBackend
from goulot.llm.budget import Budget
from goulot.llm.calls import CallSpec, LLMError, call
from goulot.llm.pricing import Usage, cost_usd
from goulot.pipeline import analysis
from goulot.pipeline.verdict import EventInput, Verdict, compute
from goulot.prices import stats
from goulot.prices import store as prices
from goulot.private import service as private_service
from goulot.vault.keys import Vault, VaultError

log = logging.getLogger("goulot.pipeline.assess")

ACTIVE_WINDOW = timedelta(days=60)
WORST_CASE = Usage(input_tokens=30_000, output_tokens=analysis.MAX_TOKENS)
VERDICT_FIELD = "verdict_inputs.v1"
BREAK_IN_DAYS = 90


@dataclass
class Allowance:
    """What the watch budget allows this cycle (lot 7 sets it from the monthly spend)."""

    analyze: bool = True
    min_severity: int = 0
    rules_only: bool = False
    one_batch_a_day: bool = False


def anomalous_materials(conn: sqlite3.Connection) -> set[str]:
    """Materials whose best daily series just moved beyond 3 standard deviations (an analysis trigger)."""
    out = set()
    for m in store.list_materials(conn):
        s = prices.best_series(conn, m["id"]) if m["status"] == "validated" else None
        z = stats.anomaly_z(s) if s is not None and s.frequency == "daily" else None
        if z is not None and abs(z) > stats.ANOMALY_Z:
            out.add(m["id"])
    return out


def due_events(conn: sqlite3.Connection, allowance: Allowance, weighs: set[str] | frozenset[str] = frozenset()
               ) -> list[sqlite3.Row]:
    """`weighs`: events that touch a site of Ma chaîne which weighs for the user (computed on the Mac): they trigger
    the analysis even under 0.5 % of world supply, and pass the crisis reserve from severity 2 (CLAUDE.md, Budget)."""
    out = []
    anomalies = anomalous_materials(conn)
    # An archived material gets no AI call (docs/SPEC.md, section 2).
    for e in conn.execute("SELECT e.* FROM events e JOIN materials m ON m.id = e.material_id "
                          "WHERE m.status = 'validated' AND e.analysis_state NOT IN ('queued') "
                          "ORDER BY e.first_seen_at"):
        priced = e["material_id"] in anomalies and e["confirmation"] == "confirmed"
        personal = e["id"] in weighs and (e["confirmation"] == "confirmed"
                                          or e["severity_hint"] >= analysis.URGENT_SEVERITY)
        floor = min(allowance.min_severity, 2) if e["id"] in weighs else allowance.min_severity
        if e["severity_hint"] < floor or not (priced or personal or analysis.should_analyze(conn, e)):
            continue
        last = conn.execute("SELECT signature FROM analyses WHERE event_id = ? ORDER BY created_at DESC LIMIT 1",
                            (e["id"],)).fetchone()
        if last is None or analysis.signature_changed(last[0], e):
            out.append(e)
    return out


def run_analyses(db: Database, budget: Budget, std: MessageBackend | None, batched: BatchBackend | None,
                 now: datetime, allowance: Allowance, budget_name: str = "watch",
                 weighs: set[str] | frozenset[str] = frozenset()) -> dict[str, int]:
    """Collect ended analysis batches, then send the due analyses. Returns counts by mode."""
    counts = {"collected": 0, "standard": 0, "batch": 0}
    if batched is not None:
        counts["collected"] = collect(db, budget, batched, budget_name)
    if not allowance.analyze or allowance.rules_only:
        return counts
    requests: list[tuple[str, dict[str, Any]]] = []
    queued: list[str] = []
    with db.transaction() as conn:
        events = due_events(conn, allowance, weighs)
        prepared = []
        for e in events:
            contexts, _, _ = prices.market(conn, e["material_id"], date.fromisoformat(e["first_seen_at"][:10]))
            found = similarity.analogues(conn, e["material_id"], similarity.target_for_event(conn, e))
            entries = similarity.dossier_entries(found, [c["instrument_id"] for c in contexts])
            prepared.append((e, analysis.build_dossier(conn, e, now, instruments=contexts, analogues=entries)))
    for event, dossier in prepared:
        urgent = event["severity_hint"] >= analysis.URGENT_SEVERITY
        if urgent and std is not None:
            with db.transaction() as conn:
                analysis_id = analysis.create(conn, event, dossier, "standard")
                parts = analysis.part_ids(conn, analysis_id)
            for part, cid in parts.items():
                spec = CallSpec(key=cid, purpose="impact", budget=budget_name,
                                max_cost_usd=cost_usd(analysis.MODEL, WORST_CASE), material_id=event["material_id"])
                try:
                    result = call(std, budget, spec, analysis.part_params(dossier, part))
                    message: dict[str, Any] | None = {"stop_reason": result.stop_reason, "content": result.content}
                    cost = result.cost_usd
                except LLMError as exc:
                    log.warning("impact analysis failed: %s", type(exc).__name__)
                    message, cost = None, 0.0
                with db.transaction() as conn:
                    analysis.store_part(conn, cid, message, cost)
            counts["standard"] += 1
        elif batched is not None:
            with db.transaction() as conn:
                analysis_id = analysis.create(conn, event, dossier, "batch")
                parts = analysis.part_ids(conn, analysis_id)
            requests += [(cid, analysis.part_params(dossier, part)) for part, cid in parts.items()]
            queued.append(analysis_id)
    if requests and batched is not None:
        worst = len(requests) * cost_usd(analysis.MODEL, WORST_CASE, batch=True)
        batch_id = batch.submit(db, budget, batched, "impact", budget_name, requests, worst)
        with db.transaction() as conn:
            conn.executemany("UPDATE analyses SET batch_id = ? WHERE id = ?", [(batch_id, a) for a in queued])
        counts["batch"] = len(queued)
    return counts


def collect(db: Database, budget: Budget, backend: BatchBackend, budget_name: str = "watch") -> int:
    """Store the parts of ended batches; returns the number of analyses completed."""
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM llm_batches WHERE purpose = 'impact' AND status = 'submitted'")]
    stored = 0
    for batch_id in ids:
        with db.connect() as conn:
            rows = {r["custom_id"]: r["material_id"] for r in conn.execute(
                "SELECT p.custom_id, a.material_id FROM analysis_parts p JOIN analyses a ON a.id = p.analysis_id "
                "WHERE a.batch_id = ?", (batch_id,))}
        results = batch.collect(db, budget, backend, batch_id, "impact", budget_name, rows)
        if results is None:
            continue
        with db.transaction() as conn:
            for r in results:
                if r.custom_id in rows:
                    cost = cost_usd(analysis.MODEL, Usage.from_api((r.message or {}).get("usage")), batch=True)
                    if analysis.store_part(conn, r.custom_id, r.message if r.ok else None, cost) is not None:
                        stored += 1
    return stored


# --- verdicts ---------------------------------------------------------------------------------------------
def event_inputs(conn: sqlite3.Connection, material_id: str, now: datetime) -> list[EventInput]:
    since = (now - ACTIVE_WINDOW).isoformat(timespec="seconds")
    out = []
    for e in conn.execute("SELECT * FROM public_events WHERE material_id = ? AND last_seen_at >= ? "
                          "AND event_status NOT IN ('resolved')", (material_id, since)):
        links = conn.execute(
            "SELECT l.node_key, n.global_share_pct FROM event_links l LEFT JOIN chain_nodes n ON n.material_id = "
            "l.material_id AND n.node_key = l.node_key AND n.valid_to IS NULL WHERE l.event_id = ?", (e["id"],)
        ).fetchall()
        shares = [r[1] for r in links if r[1] is not None]
        out.append(EventInput(
            id=e["id"], confirmed=e["confirmation"] == "confirmed", best_lineage=e["best_lineage_reliability"],
            node_keys=[r[0] for r in links], analysis=analysis.latest_valid(conn, e["id"]),
            exposed_share_pct=max(shares) if shares else None,
        ))
    return out


def _changed(conn: sqlite3.Connection, material_id: str, v: Verdict) -> bool:
    cur = conn.execute("SELECT verdict, pressure, x_weeks FROM verdicts WHERE material_id = ? AND scope = 'market' "
                       "AND current = 1", (material_id,)).fetchone()
    return cur is None or (cur[0], cur[1], cur[2]) != (v.verdict, v.pressure, v.x_weeks)


def refresh_verdicts(db: Database, vault: Vault, now: datetime, *, verified: bool, last_ok_cycle: str | None,
                     rules_only: bool = False) -> list[tuple[str, Verdict]]:
    """Market verdict of every validated material; returns the materials whose verdict changed."""
    changed = []
    with db.transaction() as conn:
        materials = [m for m in store.list_materials(conn) if m["status"] == "validated"]
        first = conn.execute("SELECT MIN(validated_at) FROM materials WHERE validated_at IS NOT NULL").fetchone()[0]
        break_in = first is None or now - datetime.fromisoformat(first) < timedelta(days=BREAK_IN_DAYS)
        for m in materials:
            chain = store.load_chain(conn, m["id"])
            _, targets, last_prices = prices.market(conn, m["id"], None)
            v = compute(event_inputs(conn, m["id"], now), commodities=set(analysis.commodities(chain["nodes"])),
                        instruments=targets, delays=delays_to_material(chain["nodes"], chain["edges"]),
                        last_prices=last_prices, verified=verified, rules_only=rules_only)
            if not _changed(conn, m["id"], v):
                continue
            inputs = {"material": m["id"], "events": [vars(e) | {"analysis": bool(e.analysis)} for e in
                                                      event_inputs(conn, m["id"], now)],
                      "verified": verified, "rules_only": rules_only, "at": now.isoformat(timespec="seconds")}
            verdict_id = str(uuid.uuid4())
            blob, version, digest = None, None, ""
            try:
                blob, version = vault.encrypt_json(VERDICT_FIELD, verdict_id, inputs)
                digest = vault.inputs_digest(inputs)
            except VaultError:
                log.info("vault unavailable: verdict inputs not kept")
            previous = conn.execute("SELECT pressure FROM verdicts WHERE material_id = ? AND scope = 'market' "
                                    "AND current = 1", (m["id"],)).fetchone()
            versions = sorted({f"{r['model']}|{r['prompt_version']}|{r['schema_version']}" for r in conn.execute(
                "SELECT model, prompt_version, schema_version FROM analyses WHERE state = 'done' AND event_id IN "
                "(SELECT value FROM json_each(?))", (json.dumps(v.event_ids),))})
            outcomes.record_verdict(conn, m["id"], v, inputs_digest=digest, versions={"analyses": versions},
                                    previous_pressure=previous[0] if previous else None, at=now)
            conn.execute("UPDATE verdicts SET current = 0 WHERE material_id = ? AND scope = 'market'", (m["id"],))
            conn.execute(
                "INSERT INTO verdicts (id, material_id, scope, verdict, pressure, x_weeks, x_low, x_high, window_weeks,"
                " window_assumed, rules_only, break_in, explanation, event_ids, inputs, inputs_key_version, "
                "inputs_digest, last_ok_cycle, created_at) VALUES (?, ?, 'market', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?, ?, ?, ?)",
                (verdict_id, m["id"], v.verdict, v.pressure, v.x_weeks, v.x_low, v.x_high, v.window_weeks,
                 int(v.window_assumed), int(v.rules_only), int(break_in), v.explanation, json.dumps(v.event_ids),
                 blob, version, digest, last_ok_cycle, utcnow()),
            )
            changed.append((m["id"], v))
    return changed


def in_break_in(db: Database, now: datetime) -> bool:
    with db.connect() as conn:
        first = conn.execute("SELECT MIN(validated_at) FROM materials WHERE validated_at IS NOT NULL").fetchone()[0]
    return first is None or now - datetime.fromisoformat(first) < timedelta(days=BREAK_IN_DAYS)


def resolve_outcomes(db: Database, now: datetime, vault: Vault | None = None) -> int:
    """Outcomes of matured advices and missed rises, on each material's best series (docs/SPEC.md, section 7).
    With the vault key, personal advices too (answers, private series, signals, public prices)."""
    with db.transaction() as conn:
        written = outcomes.resolve(conn, now.date(), lambda m: prices.best_series(conn, m), at=now)
        if vault is not None and private_service.usable(vault):
            try:
                written += ledger_personal.resolve(conn, vault, now.date(), lambda m: prices.best_series(conn, m),
                                                   at=now)
            except VaultError:
                log.warning("vault key unavailable: personal outcomes wait")
        for m in store.list_materials(conn):
            if m["status"] == "validated":
                written += outcomes.record_missed_moves(conn, m["id"], prices.best_series(conn, m["id"]), at=now)
    return written
