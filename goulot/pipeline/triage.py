"""AI triage of the candidate articles, by Claude Haiku 5.5 in Batch (docs/SPEC.md, section 4, step 3).

One request per (article, material): the model sees the public typical chain of that material, never Ma chaîne.
Outputs are validated by schema, then by code (known node ids, dates, lengths). `injection_suspected` sends the
article to quarantine: it never reaches an event.

A copy is not sent: an article telling the same story as one judged in the last 7 days for the same material (same
headline, or near-identical title and excerpt: the same lineage, `corroborate.same_story`) takes that judgment,
quarantine included, at no cost. It adds nothing to corroboration, the two being one lineage. While its twin is
still in a batch, it waits.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from goulot.chain import store
from goulot.db.connection import Database, utcnow
from goulot.llm import batch
from goulot.llm.batch import BatchBackend
from goulot.llm.budget import Budget
from goulot.llm.pricing import Usage, cost_usd
from goulot.llm.prompts import escape, load_prompt, tag
from goulot.llm.validate import DATE, ISO2, SchemaError, parse_json_output, schema, validate_schema
from goulot.pipeline.corroborate import same_story, shingles

log = logging.getLogger("goulot.pipeline.triage")

PROMPT = "triage_v1"
SCHEMA = "triage_v1"
MODEL = "claude-haiku-5-5"
MAX_TOKENS = 1_500
MAX_PER_BATCH = 200
MAX_ATTEMPTS = 3
COPY_WINDOW = timedelta(days=7)
WORST_CASE = Usage(input_tokens=4_000, output_tokens=MAX_TOKENS)
TRIAGE_ENUMS = {"event_type": "other", "event_status": "unclear"}


def custom_id(material: dict[str, Any], url_hash: str) -> str:
    return f"tri_{material['cas'].replace('-', '')}_{url_hash[:24]}"


def chain_context(nodes: list[dict[str, Any]]) -> str:
    lines = [f"{escape(n['node_key'])} | {escape(n['node_type'])} | {escape(n['name'])} | {escape(n['place'])} | "
             f"{escape(n['country_iso2'])}" for n in nodes]
    return tag("chain_context", "\n".join(lines))


def article_block(article: sqlite3.Row) -> str:
    """Whitelisted public fields of a collected article."""
    fields = [
        ("title", article["title"]), ("source", article["provider_domain"] or article["domain"]),
        ("published_at", article["published_at"] or "unknown"), ("language", article["lang"] or "unknown"),
        ("excerpt", article["excerpt"]),
    ]
    return tag("article", "\n".join(f"{k}: {escape(str(v))}" for k, v in fields))


def triage_params(context: str, article: sqlite3.Row) -> dict[str, Any]:
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": [
            {"type": "text", "text": load_prompt(PROMPT)},
            {"type": "text", "text": context, "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        ],
        "thinking": {"type": "disabled"},
        "output_config": {"format": {"type": "json_schema", "schema": schema(SCHEMA)}},
        "messages": [{"role": "user", "content": article_block(article)}],
    }


@dataclass
class Judged:
    article_id: int
    title: str
    grams: set[str]
    state: str  # pending, done or quarantined
    result: str | None


def _judged(conn: sqlite3.Connection, material_id: str, since: str) -> list[Judged]:
    """Articles judged or being judged for this material since `since`, judged ones first."""
    rows = conn.execute(
        "SELECT a.id, a.title, a.excerpt, t.state, t.result FROM triage t JOIN articles a ON a.id = t.article_id "
        "WHERE t.material_id = ? AND t.state IN ('done', 'quarantined', 'pending') AND a.first_seen_at >= ? "
        "ORDER BY t.state = 'pending', a.id", (material_id, since)).fetchall()
    return [Judged(r["id"], r["title"], shingles(f"{r['title']} {r['excerpt']}"), r["state"], r["result"])
            for r in rows]


def _twin(article: sqlite3.Row, judged: list[Judged]) -> Judged | None:
    grams = shingles(f"{article['title']} {article['excerpt']}")
    return next((j for j in judged if j.article_id != article["id"]
                 and same_story(article["title"], grams, j.title, j.grams)), None)


def _copy(conn: sqlite3.Connection, article: sqlite3.Row, material_id: str, cid: str, twin: Judged) -> None:
    now = utcnow()
    conn.execute(
        "INSERT OR IGNORE INTO triage (article_id, material_id, custom_id, model, prompt_version, schema_version, "
        "state, result, cost_usd, created_at, done_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (article["id"], material_id, cid, MODEL, PROMPT, SCHEMA, twin.state, twin.result, now, now))
    details = json.loads(article["details"] or "{}")
    details["copy_of"] = twin.article_id
    conn.execute("UPDATE articles SET details = ? WHERE id = ?", (json.dumps(details, ensure_ascii=False),
                                                                   article["id"]))


def submit_pending(db: Database, budget: Budget, backend: BatchBackend, budget_name: str = "watch") -> str | None:
    """Queue every new news or policy article for each validated material it concerns; one batch. Copies take
    their twin's judgment instead (module docstring)."""
    requests: list[tuple[str, dict[str, Any]]] = []
    with db.transaction() as conn:
        contexts: dict[str, tuple[dict[str, Any], str]] = {}
        judged: dict[str, list[Judged]] = {}
        copied: set[int] = set()
        for a in conn.execute("SELECT * FROM articles WHERE status = 'new' AND kind IN ('news', 'policy') "
                              "ORDER BY id LIMIT ?", (MAX_PER_BATCH,)).fetchall():
            for material_id in json.loads(a["materials"]):
                if material_id not in contexts:
                    material = store.get_material(conn, material_id)
                    if material is None or material["status"] != "validated":
                        continue
                    contexts[material_id] = (material, chain_context(store.load_chain(conn, material_id)["nodes"]))
                if material_id not in contexts:
                    continue
                material, context = contexts[material_id]
                cid = custom_id(material, a["url_hash"])
                if conn.execute("SELECT 1 FROM triage WHERE custom_id = ?", (cid,)).fetchone():
                    continue
                if material_id not in judged:
                    since = (datetime.fromisoformat(a["first_seen_at"]) - COPY_WINDOW).isoformat(timespec="seconds")
                    judged[material_id] = _judged(conn, material_id, since)
                twin = _twin(a, judged[material_id])
                if twin is not None and twin.state == "pending":
                    continue  # judged with its twin, once the batch ends
                if twin is not None:
                    _copy(conn, a, material_id, cid, twin)
                    copied.add(a["id"])
                    continue
                conn.execute(
                    "INSERT INTO triage (article_id, material_id, custom_id, model, prompt_version, "
                    "schema_version, state, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                    (a["id"], material_id, cid, MODEL, PROMPT, SCHEMA, utcnow()),
                )
                requests.append((cid, triage_params(context, a)))
                judged[material_id].append(Judged(a["id"], a["title"], shingles(f"{a['title']} {a['excerpt']}"),
                                                  "pending", None))
        for article_id in copied:
            _close_article(conn, article_id)
    if not requests:
        return None
    worst = len(requests) * cost_usd(MODEL, WORST_CASE, batch=True)
    batch_id = batch.submit(db, budget, backend, "triage", budget_name, requests, worst)
    with db.transaction() as conn:
        conn.executemany("UPDATE triage SET batch_id = ? WHERE custom_id = ?", [(batch_id, c) for c, _ in requests])
    return batch_id


def check_triage(result: dict[str, Any], node_keys: set[str]) -> list[str]:
    """Business checks of `schemas/check_schemas.py`; fixes what can be fixed, returns what was wrong."""
    issues = []
    for k, default in TRIAGE_ENUMS.items():
        if not isinstance(result.get(k), str):
            result[k] = default
    unknown = [n for n in result["matched_node_ids"] if n not in node_keys]
    if unknown:
        issues.append("unknown node ids")
        result["matched_node_ids"] = [n for n in result["matched_node_ids"] if n in node_keys]
    if result["country_iso2"] != "unknown" and not ISO2.match(result["country_iso2"]):
        result["country_iso2"] = "unknown"
    if result["event_date"] != "unknown" and not DATE.match(result["event_date"]):
        result["event_date"] = "unknown"
    result["future_dates"] = [d for d in result["future_dates"] if DATE.match(d["date"])]
    for key, limit in (("title_fr", 12), ("summary_fr", 30)):
        words = re.split(r"\s+", result[key].strip())
        if len(words) > limit:
            issues.append(f"{key} too long")
            result[key] = " ".join(words[:limit]) + "…"
    result["severity_hint"] = min(max(int(result["severity_hint"]), 0), 4)
    return issues


def collect_pending(db: Database, budget: Budget, backend: BatchBackend, budget_name: str = "watch") -> int:
    """Collect ended triage batches; returns the number of results stored."""
    with db.connect() as conn:
        batch_ids = [r[0] for r in conn.execute(
            "SELECT id FROM llm_batches WHERE purpose = 'triage' AND status = 'submitted'")]
    stored = 0
    for batch_id in batch_ids:
        with db.connect() as conn:
            material_of = {r[0]: r[1] for r in conn.execute(
                "SELECT custom_id, material_id FROM triage WHERE batch_id = ?", (batch_id,))}
        results = batch.collect(db, budget, backend, batch_id, "triage", budget_name, material_of)
        if results is None:
            continue
        with db.transaction() as conn:
            for r in results:
                stored += _store_result(conn, r)
            _close_articles(conn, batch_id)
    return stored


def _store_result(conn: sqlite3.Connection, r: batch.BatchResult) -> int:
    row = conn.execute("SELECT * FROM triage WHERE custom_id = ?", (r.custom_id,)).fetchone()
    if row is None:
        return 0
    now = utcnow()
    message = r.message or {}
    cost = cost_usd(MODEL, Usage.from_api(message.get("usage")), batch=True) if r.ok else 0.0
    if not r.ok or message.get("stop_reason") != "end_turn":
        conn.execute("UPDATE triage SET state = 'error', cost_usd = ?, done_at = ? WHERE id = ?",
                     (cost, now, row["id"]))
        return 0
    try:
        result = validate_schema(SCHEMA, parse_json_output(list(message.get("content") or [])))
    except SchemaError:
        conn.execute("UPDATE triage SET state = 'rejected', cost_usd = ?, done_at = ? WHERE id = ?",
                     (cost, now, row["id"]))
        return 0
    nodes = {n["node_key"] for n in store.load_chain(conn, row["material_id"])["nodes"]}
    check_triage(result, nodes)
    state = "quarantined" if result["injection_suspected"] else "done"
    conn.execute("UPDATE triage SET state = ?, result = ?, cost_usd = ?, done_at = ? WHERE id = ?",
                 (state, json.dumps(result, ensure_ascii=False), cost, now, row["id"]))
    return 1


def _close_articles(conn: sqlite3.Connection, batch_id: str) -> None:
    """An article is `quarantined` if one triage suspects an injection, `triaged` if one finds it relevant,
    `irrelevant` otherwise (kept 30 days for audit, then deleted)."""
    articles = conn.execute("SELECT DISTINCT article_id FROM triage WHERE batch_id = ?", (batch_id,)).fetchall()
    for (article_id,) in articles:
        _close_article(conn, article_id)


def _close_article(conn: sqlite3.Connection, article_id: int) -> None:
    """Once no triage of the article is pending: its status from all its triages (`_close_articles`)."""
    rows = conn.execute("SELECT state, result FROM triage WHERE article_id = ?", (article_id,)).fetchall()
    if any(r["state"] == "pending" for r in rows):
        return
    failed = [r for r in rows if r["state"] in ("error", "rejected")]
    if any(r["state"] == "quarantined" for r in rows):
        status = "quarantined"
    elif any(r["state"] == "done" and json.loads(r["result"])["relevant"] for r in rows):
        status = "triaged"
    elif failed:
        # Never judged: retried at the next cycles, at most MAX_ATTEMPTS times, then set aside with the reason.
        details = json.loads(conn.execute("SELECT details FROM articles WHERE id = ?",
                                          (article_id,)).fetchone()[0])
        details["triage_failures"] = int(details.get("triage_failures", 0)) + 1
        status = "new" if details["triage_failures"] < MAX_ATTEMPTS else "irrelevant"
        if status == "irrelevant":
            details["set_aside"] = "tri impossible après plusieurs essais"
        conn.execute("UPDATE articles SET details = ? WHERE id = ?", (json.dumps(details), article_id))
        conn.execute("DELETE FROM triage WHERE article_id = ? AND state IN ('error', 'rejected')", (article_id,))
    else:
        status = "irrelevant"
    conn.execute("UPDATE articles SET status = ? WHERE id = ?", (status, article_id))


def purge_irrelevant(conn: sqlite3.Connection, now_iso: str, days: int = 30) -> int:
    from datetime import datetime, timedelta

    limit = (datetime.fromisoformat(now_iso) - timedelta(days=days)).isoformat(timespec="seconds")
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM articles WHERE status = 'irrelevant' AND first_seen_at < ?", (limit,))]
    for article_id in ids:
        conn.execute("DELETE FROM triage WHERE article_id = ?", (article_id,))
        conn.execute("DELETE FROM articles WHERE id = ?", (article_id,))
    return len(ids)
