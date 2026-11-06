"""The analyst of each material (lot 10, docs/SPEC.md, section 4; `prompts/analyst_v1.md`).

Claude Sonnet 5.5, in a Batch, rereads what the watch knows about one material (retained and set-aside events, the
impact analyses already paid for, prices, the typical chain, the hot spots touching it, known dates and its own
previous reading) and says what is going on, what changed, what really counts, what to watch and how much to trust
it. It writes no verdict (rule 4): it may doubt the computed verdict, which the screen shows without changing it.
It does not advise either, so nothing goes to the ledger.

The dossier is built from whitelisted public fields only: `public_events` (never an event found through Ma
chaîne), the market verdict, public prices and the typical chain. Never Ma chaîne, the personal verdict, supplier
signals, quotes, stock, lead times, the delivery zone or a URL (rule 5).

When (decision of 6 November 2026): a first reading; every week, Sunday evening, for the Monday digest; when the
market verdict changed (24 h apart at least); when a serious event arrived (a confirmed public event of severity
hint ≥ 2, or an impact analysis of severity ≥ 2), 48 h apart at least. Economy mode: only a changed verdict, once a
week at most. Crisis reserve and rules only: nothing. Its own cap, 1.50 $ a month, sits inside the watch budget.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from goulot.chain import mapdata, store
from goulot.db.connection import Database, utcnow
from goulot.llm import batch
from goulot.llm.batch import BatchBackend
from goulot.llm.budget import CRISIS, ECONOMY, NORMAL, RULES_ONLY, Budget, month_start
from goulot.llm.pricing import Usage, cost_usd, monthly_budget
from goulot.llm.prompts import escape, load_prompt, tag
from goulot.llm.validate import SchemaError, parse_json_output, schema, validate_schema
from goulot.pipeline import analysis
from goulot.pipeline.visibility import from_headlines, from_old_news, minor_alert
from goulot.prices import store as prices

log = logging.getLogger("goulot.pipeline.analyst")

PROMPT = SCHEMA = "analyst_v1"
MODEL = "claude-sonnet-5-5"
MAX_TOKENS = 8_000
WORST_CASE = Usage(input_tokens=20_000, output_tokens=MAX_TOKENS)
PURPOSE = "analyst"
WINDOW = timedelta(days=60)  # the events of the Fiche matière
MAX_EVENTS, MAX_SET_ASIDE, MAX_NODES, MAX_UPCOMING = 25, 15, 20, 12
EXCERPT_CHARS = 400
VERDICT_GAP = timedelta(hours=24)
EVENT_GAP = timedelta(hours=48)
ECONOMY_GAP = timedelta(days=7)
PENDING_TIMEOUT = timedelta(hours=26)  # a Batch ends within 24 h; after that, the reading is retried
SERIOUS_SEVERITY = 2
WEEKLY_DAY, WEEKLY_HOUR = 6, 18  # Sunday evening, local time, like the digest
MAX_DAYS_AHEAD = 548  # about 18 months
LENGTH_MARGIN = 1.25  # a little over the asked length is accepted; far over, the reading is rejected
TRIGGERS = ("first", "weekly", "verdict", "event")


def monthly_cap() -> float:
    return monthly_budget("watch_analyst")


def week_of(local: datetime) -> str:
    """ISO week of the digest a reading made now feeds (Sunday evening and Monday belong to the ending week)."""
    day = local if local.weekday() == WEEKLY_DAY else local - timedelta(days=local.weekday() + 1)
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _weekly_window(local: datetime) -> bool:
    return (local.weekday(), local.hour) >= (WEEKLY_DAY, WEEKLY_HOUR) or local.weekday() == 0


def _at(text: str) -> datetime:
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# --- what the analyst reads --------------------------------------------------------------------------------
def set_aside_reason(conn: sqlite3.Connection, event: sqlite3.Row) -> str | None:
    if from_old_news(conn, event["id"]):
        return "old_news"
    if minor_alert(conn, event["id"]):
        return "minor_alert"
    if from_headlines(conn, event["id"]):
        return "headline_only"
    if event["analysis_state"] == "quarantined":
        return "quarantined"
    return None


def _verdict(conn: sqlite3.Connection, material_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM verdicts WHERE material_id = ? AND scope = 'market' AND current = 1", (material_id,)).fetchone()
    return row


def _active_events(conn: sqlite3.Connection, material_id: str, now: datetime) -> list[sqlite3.Row]:
    return list(conn.execute("SELECT * FROM public_events WHERE material_id = ? AND last_seen_at >= ? "
                             "ORDER BY first_seen_at", (material_id, (now - WINDOW).isoformat())))


def serious_keys(conn: sqlite3.Connection, material_id: str, now: datetime) -> list[str]:
    """What counts as « a serious event arrived »: confirmed public events of severity hint ≥ 2, and impact
    analyses of severity ≥ 2, still active."""
    keys = set()
    for e in _active_events(conn, material_id, now):
        if set_aside_reason(conn, e) is not None:
            continue
        if e["confirmation"] == "confirmed" and e["severity_hint"] >= SERIOUS_SEVERITY:
            keys.add(f"e:{e['id']}")
        result = analysis.latest_valid(conn, e["id"])
        if result and max((c["severity"] for c in result["commodity_impacts"]), default=0) >= SERIOUS_SEVERITY:
            keys.add(f"a:{e['id']}")
    return sorted(keys)


@dataclass
class Dossier:
    text: str
    refs: dict[str, str]  # E… and X… -> event id
    publishers: list[str]
    today: str
    serious: list[str]
    verdict_id: str | None


def _event_entry(conn: sqlite3.Connection, e: sqlite3.Row, ref: str, nodes: dict[str, dict[str, Any]],
                 verdict_events: set[str], previous_at: str | None) -> dict[str, Any]:
    links = [r[0] for r in conn.execute("SELECT node_key FROM event_links WHERE event_id = ?", (e["id"],))]
    srcs = conn.execute(
        "SELECT MIN(a.published_at) AS oldest, GROUP_CONCAT(DISTINCT COALESCE(NULLIF(a.provider_domain, ''), "
        "a.domain)) AS pubs FROM event_articles ea JOIN articles a ON a.id = ea.article_id WHERE ea.event_id = ?",
        (e["id"],)).fetchone()
    publishers = [p for p in (srcs["pubs"] or "").split(",") if p][:5]
    entry: dict[str, Any] = {
        "ref": ref, "type": e["event_type"], "origin": e["origin"], "status": e["event_status"],
        "confirmation": e["confirmation"], "c_fait": round(float(e["c_fait"]), 2), "severity_hint": e["severity_hint"],
        "lineages": e["lineages"], "official_source": bool(e["official_source"]),
        "first_seen": e["first_seen_at"][:10], "last_seen": e["last_seen_at"][:10],
        "oldest_published": (srcs["oldest"] or "unknown")[:10], "publishers": publishers,
        "place": e["place"], "country": e["country_iso2"],
        "nodes": [nodes[k]["name"] for k in links if k in nodes][:6],
        "title": e["title"], "summary": (e["summary"] or "")[:EXCERPT_CHARS],
        "in_verdict": e["id"] in verdict_events,
        "new_since_previous": previous_at is None or e["first_seen_at"] > previous_at or e["updated_at"] > previous_at,
    }
    result = analysis.latest_valid(conn, e["id"])
    if result:
        hyp = next((h for h in result.get("hypotheses", []) if h["horizon"] == "20d"
                    and h["metric"] == "outright_price"), None)
        entry["impact_analysis"] = {
            "headline": result.get("headline", ""),
            "max_severity": max((c["severity"] for c in result["commodity_impacts"]), default=0),
            "price_20d": {"p10": hyp["move_p10"], "p90": hyp["move_p90"], "probability": hyp["probability"],
                          "unit": hyp["move_unit"]} if hyp else None,
        }
    return entry


def _prices(conn: sqlite3.Connection, material_id: str) -> dict[str, Any] | None:
    best = prices.best_series(conn, material_id)
    if not best or not best.points:
        return None
    last_day, last = best.points[-1]

    def change(days: int) -> float | None:
        before = best.last_before(last_day - timedelta(days=days))
        return round(100 * (last / before[1] - 1), 2) if before and before[1] else None

    return {"instrument": best.name, "rank": best.rank, "frequency": best.frequency, "last_date": last_day.isoformat(),
            "last": last, "change_1w_pct": change(7), "change_4w_pct": change(28), "change_12w_pct": change(84)}


def _chain(chain: dict[str, Any]) -> list[dict[str, Any]]:
    kept = [n for n in chain["nodes"] if n["node_type"] in ("chokepoint", "waterway") or n["tier"] <= 1
            or (n["global_share_pct"] or 0) >= 5]
    kept.sort(key=lambda n: (n["tier"], -(n["global_share_pct"] or 0)))
    return [{"name": n["name"], "type": n["node_type"], "tier": n["tier"], "product": n["product"],
             "country": n["country_iso2"], "global_share_pct": n["global_share_pct"]} for n in kept[:MAX_NODES]]


def _hotspots(conn: sqlite3.Connection, material_id: str) -> list[dict[str, Any]]:
    """World hot spots that touch a node of the typical chain (flows are left out: they depend on the delivery
    zone, a setting of the user)."""
    data = mapdata.build(conn, material_id)
    data["zones"] = mapdata.zones(conn, material_id, [])
    mapdata.mark_exposure(data)
    touching = {i for n in data["nodes"] for i in n["exposed_by"]}
    return [{"label": z["label"], "status": z["status"], "since": z["date"]} for z in data["zones"]
            if z["kind"] == "hotspot" and z["id"] in touching][:8]


def previous_reading(conn: sqlite3.Connection, material_id: str) -> sqlite3.Row | None:
    row: sqlite3.Row | None = conn.execute(
        "SELECT * FROM analyst_notes WHERE material_id = ? AND state = 'done' ORDER BY created_at DESC, id DESC "
        "LIMIT 1", (material_id,)).fetchone()
    return row


def build_dossier(conn: sqlite3.Connection, material: dict[str, Any], now: datetime) -> Dossier:
    mid = material["id"]
    chain = store.load_chain(conn, mid)
    nodes = {n["node_key"]: n for n in chain["nodes"]}
    v = _verdict(conn, mid)
    verdict_events = set(json.loads(v["event_ids"])) if v else set()
    prev = previous_reading(conn, mid)
    previous_at = prev["created_at"] if prev else None
    retained: list[tuple[sqlite3.Row, str | None]] = []
    aside: list[tuple[sqlite3.Row, str | None]] = []
    for e in _active_events(conn, mid, now):
        reason = set_aside_reason(conn, e)
        (aside if reason else retained).append((e, reason))
    # The verdict's events first, then the most severe, then the most recent; shown in the order they appeared.
    retained.sort(key=lambda p: (p[0]["id"] not in verdict_events, -p[0]["severity_hint"],
                                 -_at(p[0]["last_seen_at"]).timestamp()))
    retained = sorted(retained[:MAX_EVENTS], key=lambda p: p[0]["first_seen_at"])
    aside = sorted(aside, key=lambda p: p[0]["last_seen_at"], reverse=True)[:MAX_SET_ASIDE]
    refs: dict[str, str] = {}
    events, publishers = [], set()
    for i, (e, _) in enumerate(retained, 1):
        refs[f"E{i}"] = e["id"]
        entry = _event_entry(conn, e, f"E{i}", nodes, verdict_events, previous_at)
        publishers.update(entry["publishers"])
        events.append(entry)
    set_aside = []
    for i, (e, reason) in enumerate(aside, 1):
        refs[f"X{i}"] = e["id"]
        set_aside.append({"ref": f"X{i}", "reason": reason, "type": e["event_type"], "title": e["title"],
                          "first_seen": e["first_seen_at"][:10]})
    by_id = {eid: ref for ref, eid in refs.items()}
    today = now.date().isoformat()
    upcoming = [{"date": d["date"], "what": d["what"]} for d in chain["dates"]]
    upcoming += [{"date": d["date"], "what": d["what"]} for e, _ in retained for d in json.loads(e["future_dates"])]
    upcoming = sorted([u for u in upcoming if str(u["date"]) >= today[:7]], key=lambda u: str(u["date"]))
    previous = None
    if prev:
        p = json.loads(prev["result"])
        previous = {"at": prev["created_at"][:10], "headline": p["headline"], "situation": p["situation"],
                    "changes": [c["text"] for c in p["changes"]], "main_signal": p["main_signal"]["text"],
                    "watch": [{"what": w["what"], "date": w["date"]} for w in p["watch"]]}
    verdict = {
        "verdict": v["verdict"] if v else "ras", "x_weeks": v["x_weeks"] if v else None,
        "x_low": v["x_low"] if v else None, "x_high": v["x_high"] if v else None,
        "pressure": v["pressure"] if v else 0, "mode": "rules_only" if v and v["rules_only"] else "ai",
        "events": [by_id[e] for e in sorted(verdict_events) if e in by_id],
    }
    hotspots = _hotspots(conn, mid)
    price = _prices(conn, mid)
    parts = [
        tag("request", escape(json.dumps({"today": today, "previous_at": previous_at[:10] if previous_at else "none"
                                          }, ensure_ascii=False))),
        tag("material", escape(json.dumps({"name": material["name"], "cas": material["cas"]}, ensure_ascii=False))),
        tag("verdict", escape(json.dumps(verdict, ensure_ascii=False))),
        tag("events", escape("\n".join(json.dumps(e, ensure_ascii=False) for e in events)) or "(aucun)"),
        tag("set_aside", escape("\n".join(json.dumps(e, ensure_ascii=False) for e in set_aside)) or "(aucun)"),
        tag("prices", escape(json.dumps(price, ensure_ascii=False)) if price else "none"),
        tag("chain", escape(json.dumps(_chain(chain), ensure_ascii=False))),
        tag("hotspots", escape(json.dumps(hotspots, ensure_ascii=False)) if hotspots else "none"),
        tag("upcoming", escape(json.dumps(upcoming[:MAX_UPCOMING], ensure_ascii=False)) if upcoming else "none"),
        tag("previous", escape(json.dumps(previous, ensure_ascii=False)) if previous else "none"),
    ]
    return Dossier("\n\n".join(parts), refs, sorted(publishers), today, serious_keys(conn, mid, now),
                   v["id"] if v else None)


def params(dossier: Dossier) -> dict[str, Any]:
    return {
        "model": MODEL, "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": load_prompt(PROMPT), "cache_control": {"type": "ephemeral", "ttl": "1h"}}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": schema(SCHEMA)}},
        "messages": [{"role": "user", "content": dossier.text}],
    }


# --- checks of a real output (same rules as schemas/check_schemas.py) --------------------------------------
DATE_RE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
URL_RE = re.compile(r"(?i)\b(?:https?|hxxps?|ftp)://|\bwww\.")
DOMAIN_RE = re.compile(r"(?i)\b[a-z0-9][a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b")
LIMITS = {"headline": 14, "situation": 80, "change": 30, "main": 60, "why_aside": 25, "counter": 30,
          "why_watch": 25, "basis": 50, "weak": 20, "doubt": 40}
COUNTS = {"changes": 3, "set_aside": 6, "counter_evidence": 3, "watch": 4, "weak_points": 3}


def words(text: str) -> int:
    return len([w for w in re.split(r"\s+", text.strip()) if w not in {"", ":", ";", "—", "-"}])


def texts(note: dict[str, Any]) -> list[str]:
    out = [note["headline"], note["situation"], note["main_signal"]["text"], note["main_signal"]["origin"],
           note["confidence"]["basis"], note["verdict_doubt"]["reason"], *note["confidence"]["weak_points"]]
    out += [c["text"] for c in note["changes"]] + [c["text"] for c in note["counter_evidence"]]
    out += [s["why"] for s in note["set_aside"]] + [x for w in note["watch"] for x in (w["what"], w["why"])]
    return out


def check_note(note: dict[str, Any], refs: set[str], publishers: set[str], today: str,
               margin: float = LENGTH_MARGIN) -> list[str]:
    """Blocking problems of a schema-valid reading: cited events that were not sent, dates out of bounds, a URL or a
    domain that was not among the publishers sent, lengths, consistency."""
    problems = []

    def too_long(text: str, key: str) -> bool:
        return words(text) > LIMITS[key] * margin

    if too_long(note["headline"], "headline") or too_long(note["situation"], "situation"):
        problems.append("length")
    if not 1 <= len(note["changes"]) <= COUNTS["changes"] or any(too_long(c["text"], "change")
                                                                  for c in note["changes"]):
        problems.append("changes")
    for key in ("set_aside", "counter_evidence", "watch"):
        if len(note[key]) > COUNTS[key]:
            problems.append(f"too many {key}")
    if len(note["confidence"]["weak_points"]) > COUNTS["weak_points"]:
        problems.append("too many weak_points")
    lengths = [(note["main_signal"]["text"], "main"), (note["confidence"]["basis"], "basis"),
               (note["verdict_doubt"]["reason"], "doubt"), *((s["why"], "why_aside") for s in note["set_aside"]),
               *((c["text"], "counter") for c in note["counter_evidence"]), *((w["why"], "why_watch")
                                                                               for w in note["watch"]),
               *((p, "weak") for p in note["confidence"]["weak_points"])]
    if any(too_long(t, k) for t, k in lengths):
        problems.append("length")
    cited = {r for part in ("changes", "counter_evidence", "watch") for item in note[part] for r in item["event_refs"]}
    cited |= set(note["main_signal"]["event_refs"]) | {s["event_ref"] for s in note["set_aside"]}
    if cited - refs:
        problems.append("unknown events")
    novelty = note["main_signal"]["novelty"]
    if (novelty == "none") != (not note["main_signal"]["event_refs"]):
        problems.append("main_signal")
    end = (date.fromisoformat(today) + timedelta(days=MAX_DAYS_AHEAD)).isoformat()
    for w in note["watch"]:
        d = w["date"]
        if d == "unknown":
            continue
        if not DATE_RE.match(d) or d < today[:len(d)] or d > end[:len(d)]:
            problems.append(f"date {d[:12]}")
    allowed = {p.lower() for p in publishers}
    for text in texts(note):
        if URL_RE.search(text) or any(m.lower() not in allowed for m in DOMAIN_RE.findall(text)):
            problems.append("url")
            break
    if note["verdict_doubt"]["doubts"] != bool(note["verdict_doubt"]["reason"].strip()):
        problems.append("verdict_doubt")
    if note["injection_suspected"]:
        problems.append("injection")
    return sorted(set(problems))


def parse(message: dict[str, Any] | None) -> dict[str, Any] | None:
    if message is None or message.get("stop_reason") != "end_turn":
        return None
    try:
        data = parse_json_output(list(message.get("content") or []))
        if isinstance(data, dict):  # enum casing is not guaranteed (CLAUDE.md, AI calls)
            for part, key in (("main_signal", "novelty"), ("confidence", "level")):
                if isinstance(data.get(part), dict) and isinstance(data[part].get(key), str):
                    data[part][key] = data[part][key].strip().lower()
        return validate_schema(SCHEMA, data)
    except SchemaError:
        return None


# --- when -------------------------------------------------------------------------------------------------
def due(conn: sqlite3.Connection, material: dict[str, Any], now: datetime, mode: str) -> str | None:
    """The trigger of a reading due now for this material, or None."""
    if mode in (CRISIS, RULES_ONLY):
        return None
    mid = material["id"]
    pending = conn.execute("SELECT created_at FROM analyst_notes WHERE material_id = ? AND state = 'pending'",
                           (mid,)).fetchall()
    if any(now - _at(p[0]) < PENDING_TIMEOUT for p in pending):
        return None
    last = conn.execute("SELECT * FROM analyst_notes WHERE material_id = ? AND state IN ('done', 'pending') "
                        "ORDER BY created_at DESC, id DESC LIMIT 1", (mid,)).fetchone()
    v = _verdict(conn, mid)
    verdict_moved = v is not None and last is not None and v["id"] != last["verdict_id"]
    if last is None:
        return None if mode == ECONOMY else "first"
    since = now - _at(last["created_at"])
    if mode == ECONOMY:
        return "verdict" if verdict_moved and since >= ECONOMY_GAP else None
    local = now.astimezone()
    if _weekly_window(local) and conn.execute(
            "SELECT 1 FROM analyst_notes WHERE material_id = ? AND week = ? AND trigger = 'weekly' "
            "AND state IN ('done', 'pending')", (mid, week_of(local))).fetchone() is None:
        return "weekly"
    if verdict_moved and since >= VERDICT_GAP:
        return "verdict"
    if since >= EVENT_GAP and set(serious_keys(conn, mid, now)) - set(json.loads(last["serious"])):
        return "event"
    return None


PRIORITY = {"verdict": 0, "event": 1, "first": 2, "weekly": 3}


def spent_this_month(conn: sqlite3.Connection, now: datetime) -> float:
    since = month_start(now).isoformat(timespec="seconds")
    return float(conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE purpose = ? AND at >= ?",
                              (PURPOSE, since)).fetchone()[0])


def custom_id(material_id: str, trigger: str, dossier: Dossier, now: datetime) -> str:
    digest = hashlib.sha256(f"{trigger}|{now.isoformat()}|{dossier.text}".encode()).hexdigest()[:12]
    return f"anl_{material_id.replace('-', '')[:16]}_{digest}"


def submit(db: Database, budget: Budget, backend: BatchBackend | None, now: datetime, mode: str = NORMAL,
           budget_name: str = "watch") -> int:
    """Send the readings due now, in one Batch, within the analyst's own monthly cap. Returns how many."""
    if backend is None or mode in (CRISIS, RULES_ONLY):
        return 0
    worst = cost_usd(MODEL, WORST_CASE, batch=True)
    prepared: list[tuple[dict[str, Any], str, Dossier]] = []
    with db.connect() as conn:
        room = monthly_cap() - spent_this_month(conn, now)
        due_now = [(m, t) for m in store.list_materials(conn) if m["status"] == "validated"
                   for t in [due(conn, m, now, mode)] if t]
        due_now.sort(key=lambda p: PRIORITY[p[1]])
        for m, trigger in due_now[:max(0, int(room // worst))]:
            prepared.append((m, trigger, build_dossier(conn, store.get_material(conn, m["id"]) or m, now)))
    if len(prepared) < len(due_now):
        log.info("analyst: %s reading(s) left for next month (own cap)", len(due_now) - len(prepared))
    if not prepared:
        return 0
    requests, rows = [], []
    week = week_of(now.astimezone())
    for m, trigger, dossier in prepared:
        cid = custom_id(m["id"], trigger, dossier, now)
        requests.append((cid, params(dossier)))
        rows.append((m["id"], trigger, week, cid, dossier.verdict_id, json.dumps(dossier.serious),
                     json.dumps({"refs": dossier.refs, "publishers": dossier.publishers, "today": dossier.today}),
                     now.astimezone(UTC).isoformat(timespec="seconds")))
    batch_id = batch.submit(db, budget, backend, PURPOSE, budget_name, requests, len(requests) * worst)
    with db.transaction() as conn:
        conn.executemany(
            "INSERT INTO analyst_notes (material_id, trigger, week, custom_id, verdict_id, serious, dossier, "
            "created_at, batch_id, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
            [(*r, batch_id) for r in rows])
    return len(requests)


def store_result(conn: sqlite3.Connection, custom: str, message: dict[str, Any] | None, cost: float,
                 ok: bool = True) -> str | None:
    row = conn.execute("SELECT * FROM analyst_notes WHERE custom_id = ?", (custom,)).fetchone()
    if row is None:
        return None
    sent = json.loads(row["dossier"])
    result = parse(message) if ok else None
    problems = (check_note(result, set(sent["refs"]), set(sent["publishers"]), sent["today"]) if result
                else ["no valid answer"])
    state = "error" if not ok else ("done" if not problems else "rejected")
    conn.execute("UPDATE analyst_notes SET state = ?, result = ?, problems = ?, cost_usd = ?, done_at = ? "
                 "WHERE custom_id = ?", (state, json.dumps(result, ensure_ascii=False) if state == "done" else None,
                                         "; ".join(problems)[:300] if state != "done" else "", cost, utcnow(),
                                         custom))
    return state


def collect(db: Database, budget: Budget, backend: BatchBackend, budget_name: str = "watch") -> int:
    with db.connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM llm_batches WHERE purpose = ? AND status = 'submitted'",
                                          (PURPOSE,))]
        material_of = {r[0]: r[1] for r in conn.execute(
            "SELECT custom_id, material_id FROM analyst_notes WHERE state = 'pending'")}
    done = 0
    for batch_id in ids:
        results = batch.collect(db, budget, backend, batch_id, PURPOSE, budget_name, material_of)
        if results is None:
            continue
        with db.transaction() as conn:
            for r in results:
                cost = cost_usd(MODEL, Usage.from_api((r.message or {}).get("usage")), batch=True) if r.ok else 0.0
                done += store_result(conn, r.custom_id, r.message, cost, ok=r.ok) == "done"
    return done


def run(db: Database, budget: Budget, backend: BatchBackend | None, now: datetime, mode: str,
        budget_name: str = "watch") -> int:
    if backend is None:
        return 0
    collect(db, budget, backend, budget_name)
    return submit(db, budget, backend, now, mode, budget_name)


# --- what the screen and the digest show ------------------------------------------------------------------
def latest(conn: sqlite3.Connection, material_id: str) -> dict[str, Any] | None:
    """The last valid reading, with its references turned into event ids, and whether the verdict changed since."""
    row = previous_reading(conn, material_id)
    if row is None:
        return None
    refs: dict[str, str] = json.loads(row["dossier"])["refs"]
    note = json.loads(row["result"])

    def ids(rs: list[str]) -> list[str]:
        return [refs[r] for r in rs if r in refs]

    for part in ("changes", "counter_evidence", "watch"):
        for item in note[part]:
            item["event_ids"] = ids(item.pop("event_refs"))
    note["main_signal"]["event_ids"] = ids(note["main_signal"].pop("event_refs"))
    for s in note["set_aside"]:
        s["event_id"] = refs.get(s.pop("event_ref"), "")
    v = _verdict(conn, material_id)
    pending = conn.execute("SELECT 1 FROM analyst_notes WHERE material_id = ? AND state = 'pending'",
                           (material_id,)).fetchone() is not None
    titles = {r["id"]: r["title"] for r in conn.execute(
        "SELECT id, title FROM public_events WHERE material_id = ?", (material_id,)) if r["id"] in refs.values()}
    return {"created_at": row["created_at"], "trigger": row["trigger"], "note": note, "pending": pending,
            "titles": titles, "verdict_changed_since": v is not None and v["id"] != row["verdict_id"]}
