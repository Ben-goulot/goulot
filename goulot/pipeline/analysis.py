"""Impact analysis by Claude Sonnet 5.5 (docs/SPEC.md, section 4, steps 6 and 7; `prompts/impact_v1.md`).

The dossier is built from whitelisted public fields only: the event, its sources (S…), the typical chain nodes it
touches and their neighbours (R…), the tracked commodities, the market context (instruments, lot 5) and verified
analogues (phase 3, continued). Never Ma chaîne, never purchase data, never a verdict's inputs.

Triggers: a confirmed event (or an early signal with a severity hint of 3 or more) touching a node that weighs at
least 0.5 % of world supply at its level, a node whose share is unknown, or a critical node (strait, waterway).
Batch by default; standard API when the severity hint reaches 3. Quarantine: `injection_suspected`, or a
severity of 3 or more resting on a single non-official lineage. A quarantined analysis never changes a verdict.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from goulot.chain import store
from goulot.db.connection import utcnow
from goulot.ledger import outcomes
from goulot.llm.prompts import escape, load_prompt, tag
from goulot.llm.validate import SchemaError, parse_json_output, schema, validate_schema

PROMPT = "impact_v1"
SCHEMA = "impact_assessment_v1"  # the merged assessment; each call uses a part schema
MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 24_000
MIN_SHARE_PCT = 0.5
CRITICAL_SHARE_PCT = 50.0  # a node above half of its level is a near-sole supplier
URGENT_SEVERITY = 3
MAX_SOURCES = 12
EXCERPT_CHARS = 800


@dataclass
class Dossier:
    text: str
    sources: list[str]  # S… ids sent
    references: list[str]  # R… ids sent
    instruments: list[str]
    commodities: list[str]
    signature: str


def signature(event: sqlite3.Row) -> str:
    return f"{event['confirmation']}|{event['lineages']}|{event['severity_hint']}|{event['event_status']}"


# From the third independent lineage on, one more press echo leaves the confirmation and the facts of the dossier
# much the same: the event is analysed again for a change of confirmation, severity or status, not for each new
# echo (docs/DECISIONS.md, 29 October 2026). Signatures are stored whole: earlier analyses keep matching.
LINEAGE_CAP = 3


def signature_changed(previous: str, event: sqlite3.Row) -> bool:
    def key(sig: str) -> tuple[str, int, str, str] | None:
        parts = sig.split("|")
        if len(parts) != 4 or not parts[1].isdigit():
            return None
        return parts[0], min(int(parts[1]), LINEAGE_CAP), parts[2], parts[3]

    old = key(previous)
    return old is None or old != key(signature(event))


def should_analyze(conn: sqlite3.Connection, event: sqlite3.Row) -> bool:
    if event["confirmation"] != "confirmed" and event["severity_hint"] < URGENT_SEVERITY:
        return False
    rows = conn.execute(
        "SELECT n.node_type, n.global_share_pct FROM event_links l JOIN chain_nodes n ON n.material_id = l.material_id "
        "AND n.node_key = l.node_key AND n.valid_to IS NULL WHERE l.event_id = ?", (event["id"],)).fetchall()
    for r in rows:
        share = r["global_share_pct"]
        # An unknown share is not a zero share: many sites have no published figure (decision of 2026-10-18).
        if r["node_type"] in ("chokepoint", "waterway") or share is None or share >= MIN_SHARE_PCT:
            return True
    return False


def commodities(nodes: list[dict[str, Any]]) -> list[str]:
    """Commodity codes: the purchased material and its direct inputs (tier-1 material nodes)."""
    return [n["node_key"] for n in nodes if n["node_type"] == "material" and n["tier"] in (0, 1)]


def build_dossier(conn: sqlite3.Connection, event: sqlite3.Row, now: datetime,
                  instruments: list[dict[str, Any]] | None = None,
                  analogues: list[dict[str, Any]] | None = None) -> Dossier:
    chain = store.load_chain(conn, event["material_id"])
    nodes = {n["node_key"]: n for n in chain["nodes"]}
    tracked = commodities(chain["nodes"])
    linked = [r[0] for r in conn.execute("SELECT node_key FROM event_links WHERE event_id = ?", (event["id"],))]
    neighbours = {e["from_key"] for e in chain["edges"] if e["to_key"] in linked} | {
        e["to_key"] for e in chain["edges"] if e["from_key"] in linked}
    ref_keys = [k for k in [*linked, *sorted(neighbours - set(linked))] if k in nodes][:20]

    articles = conn.execute(
        "SELECT a.title, a.excerpt, a.domain, a.provider_domain, a.published_at, a.first_seen_at, "
        "ea.lineage_key, ea.reliability FROM event_articles ea JOIN articles a ON a.id = ea.article_id "
        "WHERE ea.event_id = ? ORDER BY ea.reliability DESC, a.first_seen_at LIMIT ?",
        (event["id"], MAX_SOURCES)).fetchall()
    request = {"event_id": event["id"], "t_analysis": now.isoformat(timespec="seconds"), "tracked_commodities": tracked}
    ev = {
        "type": event["event_type"], "status": event["event_status"], "title": event["title"],
        "summary": event["summary"], "place": event["place"], "country": event["country_iso2"],
        "first_seen": event["first_seen_at"], "last_seen": event["last_seen_at"], "C_fait": event["c_fait"],
        "lineages": event["lineages"], "confirmation": event["confirmation"],
    }
    source_lines = []
    for i, a in enumerate(articles, 1):
        source_lines.append(json.dumps({
            "id": f"S{i}", "lineage": a["lineage_key"][:8], "reliability": a["reliability"],
            "publisher": a["provider_domain"] or a["domain"], "published_at": a["published_at"] or a["first_seen_at"],
            "title": a["title"], "excerpt": a["excerpt"][:EXCERPT_CHARS],
        }, ensure_ascii=False))
    ref_lines = []
    for i, key in enumerate(ref_keys, 1):
        n = nodes[key]
        ref_lines.append(json.dumps({
            "id": f"R{i}", "node": key, "type": n["node_type"], "name": n["name"], "product": n["product"],
            "country": n["country_iso2"], "place": n["place"], "global_share_pct": n["global_share_pct"],
            "capacity": n["capacity_text"], "buffer_days": n["buffer_days"],
            "buffer_is_default": bool(n["buffer_is_default"]), "confidence": n["confidence"],
            "touched_by_event": key in linked,
        }, ensure_ascii=False))
    market = instruments or []
    parts = [
        tag("analysis_request", escape(json.dumps(request, ensure_ascii=False))),
        tag("event", escape(json.dumps(ev, ensure_ascii=False))),
        tag("sources", escape("\n".join(source_lines)) or "(aucune)"),
        tag("reference_data", escape("\n".join(ref_lines)) or "(aucune)"),
        tag("market_context", escape(json.dumps(market, ensure_ascii=False)) if market
            else "Aucun instrument de prix disponible : ne formule aucune hypothèse."),
        tag("analogues", escape(json.dumps(analogues or [], ensure_ascii=False)) if analogues
            else "Aucun cas comparable vérifié."),
    ]
    return Dossier("\n\n".join(parts), [f"S{i}" for i in range(1, len(articles) + 1)],
                   [f"R{i}" for i in range(1, len(ref_keys) + 1)], [m["instrument_id"] for m in market], tracked,
                   signature(event))


# The whole impact schema is too large for the API to compile (2026-10-18): the same dossier is answered in three
# partial calls of one Batch, merged and checked by the code. Part schemas are cut from impact_assessment_v1.
PARTS: dict[str, tuple[str, ...]] = {
    "comprehension": ("event_understanding", "confidence", "data_gaps", "assessment_status", "headline", "summary"),
    "impacts": ("commodity_impacts", "monitoring_triggers"),
    "hypotheses": ("hypotheses",),
}
PARTS_PROMPT = "impact_parts_v1"


def part_instruction(part: str) -> str:
    text = load_prompt(PARTS_PROMPT)
    section = text.split(f"## {part}\n", 1)[1]
    return section.split("\n## ", 1)[0].strip()


def part_params(dossier: Dossier, part: str) -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": load_prompt(PROMPT), "cache_control": {"type": "ephemeral", "ttl": "1h"}},
                   {"type": "text", "text": part_instruction(part)}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high",
                          "format": {"type": "json_schema", "schema": schema(f"impact_part_{part}_v1")}},
        "messages": [{"role": "user", "content": dossier.text}],
    }


def analysis_params(dossier: Dossier) -> dict[str, Any]:
    """The first part's request (kept for the dossier checks of the tests)."""
    return part_params(dossier, "comprehension")


def split(assessment: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The parts of a whole assessment (the inverse of merge; used by the tests and recorded fixtures)."""
    return {part: {"schema_version": f"{part}-1.0", "event_id": assessment["event_id"],
                   **{f: assessment[f] for f in fields}, "injection_suspected": assessment["injection_suspected"]}
            for part, fields in PARTS.items()}


def parse_part(part: str, message: dict[str, Any] | None) -> dict[str, Any] | None:
    if message is None or message.get("stop_reason") != "end_turn":
        return None
    try:
        return validate_schema(f"impact_part_{part}_v1", parse_json_output(list(message.get("content") or [])))
    except SchemaError:
        return None


def merge(parts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """One impact_assessment_v1 object from the three parts; the conclusion is made consistent with the
    severities, which are what the verdict uses."""
    ids = {p["event_id"] for p in parts.values()}
    merged: dict[str, Any] = {"schema_version": "1.0", "event_id": ids.pop() if len(ids) == 1 else ""}
    for part, fields in PARTS.items():
        for f in fields:
            merged[f] = parts[part][f]
    merged["injection_suspected"] = any(p["injection_suspected"] for p in parts.values())
    severities = [ci["severity"] for ci in merged["commodity_impacts"]]
    if merged["assessment_status"] == "no_material_impact" and any(severities):
        merged["assessment_status"] = "impact_assessed"
    elif merged["assessment_status"] == "impact_assessed" and not any(severities):
        merged["assessment_status"] = "no_material_impact"
    return merged


def check_impact(result: dict[str, Any], event_id: str, sent: dict[str, list[str]]) -> list[str]:
    """Business checks of `schemas/check_schemas.py` on a real output. Returns blocking problems."""
    problems = []
    if result["event_id"] != event_id:
        problems.append("event_id")
    if len(result["hypotheses"]) > 4:
        problems.append("too many hypotheses")
    for h in result["hypotheses"]:
        consistent = 0 <= h["probability"] <= 1 and h["move_p10"] <= h["move_p90"]
        if not consistent or h["instrument_id"] not in sent["instruments"]:
            problems.append(f"hypothesis {h['hypothesis_id']}")
    understanding = result["event_understanding"]
    cited = {s for item in understanding["facts"] + understanding["contradictions"] for s in item["source_ids"]}
    if cited - set(sent["sources"]) - set(sent["references"]):  # a fact may come from the reference data
        problems.append("unknown sources")
    refs = {e["reference_id"] for e in understanding["affected_entities"] if e["reference_id"] != "unmapped"}
    refs |= {r for ci in result["commodity_impacts"] for r in ci["supply_at_risk"]["reference_ids"]}
    if refs - set(sent["references"]):
        problems.append("unknown references")
    for ci in result["commodity_impacts"]:
        if ci["commodity"] not in sent["commodities"]:
            problems.append(f"unknown commodity {ci['commodity'][:30]}")
        d = ci["expected_duration_days"]
        if d["low"] is not None and d["high"] is not None and d["low"] > d["high"]:
            problems.append("duration")
        share = ci["supply_at_risk"]["global_share_pct"]
        if share is not None and not 0 <= share <= 100:
            problems.append("share")
    severities = [ci["severity"] for ci in result["commodity_impacts"]]
    if result["assessment_status"] == "no_material_impact" and any(severities):
        problems.append("no_material_impact with severity")
    if not 0 <= result["confidence"]["analysis_confidence"] <= 1:
        problems.append("confidence")
    return problems


def quarantine_reason(result: dict[str, Any], event: sqlite3.Row) -> str:
    if result["injection_suspected"]:
        return "contenu piégé signalé par l'IA"
    high = any(ci["severity"] >= 3 for ci in result["commodity_impacts"])
    if high and event["lineages"] <= 1 and not event["official_source"]:
        return "sévérité élevée appuyée sur une seule source non officielle"
    return ""


def custom_id(event_id: str, sig: str) -> str:
    """Deterministic, so that a recorded answer is found again when the same dossier is replayed."""
    return f"ana_{event_id}_{hashlib.sha256(sig.encode()).hexdigest()[:10]}"


def create(conn: sqlite3.Connection, event: sqlite3.Row, dossier: Dossier, mode: str) -> str:
    analysis_id = str(uuid.uuid4())
    sent = {"sources": dossier.sources, "references": dossier.references, "instruments": dossier.instruments,
            "commodities": dossier.commodities}
    conn.execute(
        "INSERT INTO analyses (id, event_id, material_id, model, prompt_version, schema_version, mode, state, "
        "custom_id, signature, dossier_sources, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
        (analysis_id, event["id"], event["material_id"], MODEL, PROMPT, SCHEMA, mode,
         custom_id(event["id"], dossier.signature), dossier.signature, json.dumps(sent), utcnow()),
    )
    base = custom_id(event["id"], dossier.signature)
    for part in PARTS:
        conn.execute("INSERT INTO analysis_parts (analysis_id, part, custom_id, state) VALUES (?, ?, ?, 'pending')",
                     (analysis_id, part, f"{base}_{part[:4]}"))
    conn.execute("UPDATE events SET analysis_state = 'queued' WHERE id = ?", (event["id"],))
    return analysis_id


def part_ids(conn: sqlite3.Connection, analysis_id: str) -> dict[str, str]:
    return {r["part"]: r["custom_id"] for r in conn.execute(
        "SELECT part, custom_id FROM analysis_parts WHERE analysis_id = ?", (analysis_id,))}


def store_part(conn: sqlite3.Connection, custom: str, message: dict[str, Any] | None, cost: float) -> str | None:
    """Keep one part's answer; when every part has answered, merge and store the assessment. Returns the final
    state of the analysis once complete, else None."""
    row = conn.execute("SELECT * FROM analysis_parts WHERE custom_id = ?", (custom,)).fetchone()
    if row is None:
        return None
    result = parse_part(row["part"], message)
    conn.execute("UPDATE analysis_parts SET state = ?, result = ?, cost_usd = ? WHERE custom_id = ?",
                 ("done" if result else "error", json.dumps(result, ensure_ascii=False) if result else None, cost,
                  custom))
    rows = conn.execute("SELECT * FROM analysis_parts WHERE analysis_id = ?", (row["analysis_id"],)).fetchall()
    if any(r["state"] == "pending" for r in rows):
        return None
    total = sum(r["cost_usd"] for r in rows)
    if any(r["state"] == "error" for r in rows):
        return store_assessment(conn, row["analysis_id"], None, total, failed=True)
    merged = merge({r["part"]: json.loads(r["result"]) for r in rows})
    return store_assessment(conn, row["analysis_id"], merged, total)


def store_result(conn: sqlite3.Connection, analysis_id: str, message: dict[str, Any] | None, cost: float) -> str:
    """A complete assessment in one message (impact_assessment_v1), e.g. a replayed full answer."""
    result = None
    if message is not None and message.get("stop_reason") == "end_turn":
        try:
            result = validate_schema(SCHEMA, parse_json_output(list(message.get("content") or [])))
        except SchemaError:
            return store_assessment(conn, analysis_id, None, cost, failed=False)
    return store_assessment(conn, analysis_id, result, cost, failed=result is None)


def store_assessment(conn: sqlite3.Connection, analysis_id: str, result: dict[str, Any] | None, cost: float,
                     *, failed: bool = False) -> str:
    """Check (schema of the whole assessment, then business rules) and store; returns the final state."""
    row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    event = conn.execute("SELECT * FROM events WHERE id = ?", (row["event_id"],)).fetchone()
    now = utcnow()
    state, reason = ("error" if failed else "rejected"), ""
    if result is not None:
        try:
            result = validate_schema(SCHEMA, result)
            problems = check_impact(result, row["event_id"], json.loads(row["dossier_sources"]))
            reason = "; ".join(problems) if problems else quarantine_reason(result, event)
            state = "rejected" if problems else ("quarantined" if reason else "done")
        except SchemaError:
            state = "rejected"
    conn.execute("UPDATE analyses SET state = ?, result = ?, quarantine_reason = ?, cost_usd = ?, done_at = ? "
                 "WHERE id = ?", (state, json.dumps(result, ensure_ascii=False) if result else None, reason[:300],
                                  cost, now, analysis_id))
    conn.execute("UPDATE events SET analysis_state = ? WHERE id = ?", (state, row["event_id"]))
    if state == "done" and result is not None:
        outcomes.record_hypotheses(conn, analysis_id, row["material_id"], result,
                                   {"model": row["model"], "prompt": row["prompt_version"],
                                    "schema": row["schema_version"]})
        for h in result["hypotheses"]:
            conn.execute(
                "INSERT INTO hypotheses (analysis_id, hypothesis_id, instrument_id, metric, direction, horizon, "
                "probability, move_p10, move_p90, move_unit, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (analysis_id, h["hypothesis_id"], h["instrument_id"], h["metric"], h["direction"], h["horizon"],
                 h["probability"], h["move_p10"], h["move_p90"], h["move_unit"], now),
            )
    return state


def latest_valid(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT result FROM analyses WHERE event_id = ? AND state = 'done' ORDER BY created_at DESC "
                       "LIMIT 1", (event_id,)).fetchone()
    return json.loads(row[0]) if row else None
