"""Weekly digest (docs/SPEC.md, section 9; `prompts/digest_v1.md`).

Sunday from 18:00 (local time): one Batch request per validated material, unless the watch budget is in economy
mode or beyond. Monday from 08:00, or at the first cycle after (the Mac may sleep): one message, where the code
writes the verdict, X and the action window itself, adds the week's resolved advices and the ledger's last
fingerprint (an outside, dated copy of the chain), and the model's text only where it passed every check. If the
model disagrees with the verdict, the message says so without changing it. Each material's verdict at digest time
is written to the ledger.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from goulot.alerts.defang import defang
from goulot.chain import store
from goulot.db.connection import Database, utcnow
from goulot.ledger import chain, outcomes
from goulot.llm import batch
from goulot.llm.batch import BatchBackend
from goulot.llm.budget import Budget
from goulot.llm.pricing import Usage, cost_usd
from goulot.llm.prompts import escape, load_prompt, tag
from goulot.llm.validate import DATE, SchemaError, parse_json_output, schema, validate_schema
from goulot.prices import store as prices

log = logging.getLogger("goulot.digest")

PROMPT = SCHEMA = "digest_v1"
MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 4_000
WORST_CASE = Usage(input_tokens=8_000, output_tokens=MAX_TOKENS)
SUBMIT_DAY, SUBMIT_HOUR = 6, 18  # Sunday evening, local time
SEND_DAY, SEND_HOUR = 0, 8  # Monday morning


def week_of(local: datetime) -> str:
    year, week, _ = local.isocalendar()
    return f"{year}-W{week:02d}"


def _verdict_row(conn: sqlite3.Connection, material_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM verdicts WHERE material_id = ? AND scope = 'market' AND current = 1", (material_id,)).fetchone()
    return row


def digest_params(conn: sqlite3.Connection, material: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Whitelisted public inputs only: no purchase data, no Ma chaîne, no verdict inputs."""
    v = _verdict_row(conn, material["id"])
    events = [dict(id=e["id"], status=e["event_status"], confirmation=e["confirmation"], c_fait=e["c_fait"],
                   severity_hint=e["severity_hint"], title=e["title"])
              for e in conn.execute("SELECT * FROM events WHERE material_id = ? AND last_seen_at >= ?",
                                    (material["id"], (now - timedelta(days=7)).isoformat()))]
    best = prices.best_series(conn, material["id"])
    price = None
    if best and best.points:
        last = best.points[-1][1]
        week = best.last_before(best.points[-1][0] - timedelta(days=7))
        month = best.last_before(best.points[-1][0] - timedelta(days=28))
        price = {"instrument": best.name, "rank": best.rank, "last": last,
                 "change_1w_pct": round(100 * (last / week[1] - 1), 2) if week else None,
                 "change_4w_pct": round(100 * (last / month[1] - 1), 2) if month else None}
    previous = conn.execute("SELECT result FROM digests WHERE material_id = ? AND state = 'done' ORDER BY week DESC "
                            "LIMIT 1", (material["id"],)).fetchone()
    prev = json.loads(previous[0]) if previous else None
    upcoming = [{"date": d["date"], "what": d["what"]} for d in store.load_chain(conn, material["id"])["dates"]]
    parts = [
        tag("material", escape(json.dumps({"name": material["name"], "cas": material["cas"]}, ensure_ascii=False))),
        tag("verdict", escape(json.dumps({
            "verdict": v["verdict"] if v else "ras", "x_weeks": v["x_weeks"] if v else None,
            "mode": "rules_only" if v and v["rules_only"] else "ai", "events": json.loads(v["event_ids"]) if v else [],
        }, ensure_ascii=False))),
        tag("events", escape(json.dumps(events, ensure_ascii=False))),
        tag("prices", escape(json.dumps(price, ensure_ascii=False)) if price else "none"),
        tag("upcoming", escape(json.dumps(upcoming, ensure_ascii=False))),
        tag("previous_digest", escape(json.dumps({"headline": prev["headline"], "what_changed": prev["what_changed"]},
                                                 ensure_ascii=False)) if prev else "none"),
    ]
    return {
        "model": MODEL, "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": load_prompt(PROMPT)}],
        "thinking": {"type": "between_tools"},
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": schema(SCHEMA)}},
        "messages": [{"role": "user", "content": "\n\n".join(parts)}],
    }


def check_digest(d: dict[str, Any]) -> list[str]:
    problems = []

    def words(t: str) -> int:
        return len(t.split())

    if words(d["headline"]) > 12:
        problems.append("headline")
    if not 1 <= len(d["what_changed"]) <= 3 or any(words(x) > 25 for x in d["what_changed"]):
        problems.append("what_changed")
    if words(d["verdict_explanation"]) > 50 or len(d["watch_next"]) > 3:
        problems.append("length")
    if any(w["date"] != "unknown" and not DATE.match(w["date"]) for w in d["watch_next"]):
        problems.append("date")
    if d["disagrees_with_verdict"] != bool(d["disagreement_reason"].strip()):
        problems.append("disagreement")
    if d["injection_suspected"]:
        problems.append("injection")
    return problems


def submit(db: Database, budget: Budget, backend: BatchBackend | None, now: datetime, *, ai: bool,
           budget_name: str = "watch") -> int:
    local = now.astimezone()
    if (local.weekday(), local.hour) < (SUBMIT_DAY, SUBMIT_HOUR) and local.weekday() != SEND_DAY:
        return 0
    week = week_of(local if local.weekday() == SUBMIT_DAY else local - timedelta(days=local.weekday() + 1))
    requests: list[tuple[str, dict[str, Any]]] = []
    with db.transaction() as conn:
        for m in store.list_materials(conn):
            if m["status"] != "validated":
                continue
            cid = f"dig_{week.replace('-', '')}_{m['id'].replace('-', '')[:16]}"
            state = "pending" if ai and backend is not None else "skipped"
            cur = conn.execute("INSERT OR IGNORE INTO digests (material_id, week, custom_id, state, created_at) VALUES "
                               "(?, ?, ?, ?, ?)", (m["id"], week, cid, state, utcnow()))
            if cur.rowcount and state == "pending":
                requests.append((cid, digest_params(conn, store.get_material(conn, m["id"]) or m, now)))
    if requests and backend is not None:
        batch_id = batch.submit(db, budget, backend, "digest", budget_name, requests,
                                len(requests) * cost_usd(MODEL, WORST_CASE, batch=True))
        with db.transaction() as conn:
            conn.executemany("UPDATE digests SET batch_id = ? WHERE custom_id = ?",
                             [(batch_id, c) for c, _ in requests])
    return len(requests)


def collect(db: Database, budget: Budget, backend: BatchBackend, budget_name: str = "watch") -> int:
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM llm_batches WHERE purpose = 'digest' AND status = 'submitted'")]
    done = 0
    for batch_id in ids:
        results = batch.collect(db, budget, backend, batch_id, "digest", budget_name)
        if results is None:
            continue
        with db.transaction() as conn:
            for r in results:
                state, result = "rejected", None
                if r.ok and r.message and r.message.get("stop_reason") == "end_turn":
                    try:
                        result = validate_schema(SCHEMA, parse_json_output(list(r.message.get("content") or [])))
                        state = "rejected" if check_digest(result) else "done"
                    except SchemaError:
                        state = "rejected"
                cost = cost_usd(MODEL, Usage.from_api((r.message or {}).get("usage")), batch=True)
                conn.execute("UPDATE digests SET state = ?, result = ?, cost_usd = ? WHERE custom_id = ?",
                             (state, json.dumps(result, ensure_ascii=False) if state == "done" else None, cost,
                              r.custom_id))
                done += state == "done"
    return done


def compose(db: Database, now: datetime) -> tuple[str, str] | None:
    """(week, text) of the digest due now, or None. Writes each material's verdict to the ledger."""
    local = now.astimezone()
    if local.weekday() == SUBMIT_DAY or (local.weekday() == SEND_DAY and local.hour < SEND_HOUR):
        return None
    week = week_of(local - timedelta(days=local.weekday() + 1))
    with db.transaction() as conn:
        rows = conn.execute("SELECT * FROM digests WHERE week = ? AND sent_at IS NULL", (week,)).fetchall()
        if not rows or (any(r["state"] == "pending" for r in rows) and local - timedelta(hours=12) < local.replace(
                hour=SEND_HOUR, minute=0)):
            return None  # wait for the Batch, at most until 20:00 on Monday
        lines = [f"Goulot · synthèse de la semaine {week}"]
        for r in rows:
            material = store.get_material(conn, r["material_id"])
            v = _verdict_row(conn, r["material_id"])
            if material is None:
                continue
            label = "Pas encore de verdict." if v is None else _label(v)
            lines.append(f"\n{material['name']} : {label}")
            if v is not None and v["verdict"] in ("ant", "pot") and v["window_weeks"] is not None:
                lines.append(f"Fenêtre d'action : {max(v['window_weeks'], 0):.0f} semaines"
                             + (" (délai fournisseur supposé)." if v["window_assumed"] else "."))
            if r["state"] == "done" and r["result"]:
                d = json.loads(r["result"])
                lines.append(defang(d["headline"]))
                lines += [f"• {defang(x)}" for x in d["what_changed"]]
                lines.append(defang(d["verdict_explanation"]))
                lines.append(defang(d["price_comment"]))
                if d["disagrees_with_verdict"]:
                    lines.append(f"L'IA conteste ce verdict, qui reste inchangé : {defang(d['disagreement_reason'])}")
            else:
                lines.append("Synthèse rédigée par le code seul (mode économie ou réponse de l'IA écartée).")
            if v is not None:
                outcomes.record_verdict(conn, r["material_id"], _VerdictView(v), inputs_digest=v["inputs_digest"],
                                        versions={"digest": week}, previous_pressure=v["pressure"], at=now)
        week_start = (now - timedelta(days=7)).isoformat()
        resolved = [o for o in chain.entries(conn, kinds=("outcome",)) if o["at"] >= week_start]
        good = sum(o["result"] in ("confirmed", "correct_calm", "range_hit", "right_direction") for o in resolved)
        lines.append(f"\nBilan : {len(resolved)} conseil(s) arrivé(s) à échéance cette semaine, {good} juste(s).")
        lines.append(f"Empreinte du registre : {outcomes.latest_head(conn)[:32]}")
        conn.execute("UPDATE digests SET sent_at = ? WHERE week = ?", (utcnow(), week))
    return week, "\n".join(lines)


def _label(v: sqlite3.Row) -> str:
    if v["verdict"] == "pot" and v["x_weeks"]:
        return f"Problème potentiel à {v['x_weeks']:.0f} semaines."
    return {"ant": "Anticiper stock.", "ras": "Rien à signaler.", "unverified": "Non vérifié."}.get(v["verdict"], "")


class _VerdictView:
    """A stored verdict row seen as the Verdict fields the ledger writes."""

    def __init__(self, row: sqlite3.Row) -> None:
        self.verdict, self.pressure = row["verdict"], row["pressure"]
        self.x_weeks, self.x_low, self.x_high = row["x_weeks"], row["x_low"], row["x_high"]
        self.window_weeks, self.rules_only = row["window_weeks"], bool(row["rules_only"])
        self.event_ids = json.loads(row["event_ids"])
        self.label = _label(row)
