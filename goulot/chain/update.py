"""Bringing a typical chain up to date (docs/SPEC.md, section 3, « Mise à jour de la chaîne »).

Two kinds, each with a quote and a confirmation, on the reconstruction cap:
- `reread`: the stored research report is read again with the current rules (every producer named, status of each
  site, basis and year of each share). No web search: a few cents.
- `search`: a short web search for what changed since the last research (`chain_update_research_v1`, 6 searches at
  most), then the same reading of that new report.
Both end in proposals (`chain_update_v1`), checked by code, that the user accepts or rejects one by one on the
« Chaîne » screen. Nothing changes in the chain without that decision; an accepted proposal is a bitemporal revision,
like a correction. The model sees only the public typical chain, never Ma chaîne.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from goulot.chain import research, store
from goulot.chain.leadtimes import DEFAULT_BUFFER_DAYS, DEFAULT_LEAD_DAYS
from goulot.db.connection import Database, utcnow
from goulot.llm.backend import MessageBackend
from goulot.llm.budget import Budget, BudgetExceeded, CircuitOpen
from goulot.llm.calls import CallSpec, LLMError, call
from goulot.llm.pricing import Usage, cost_usd, web_search_price
from goulot.llm.prompts import escape, load_prompt, material_block, sources_block, tag
from goulot.llm.validate import DATE, ISO2, NODE_ID, SchemaError, parse_json_output, schema, validate_schema

log = logging.getLogger("goulot.chain.update")

RESEARCH_PROMPT = "chain_update_research_v1"
PROMPT = "chain_update_v1"
SCHEMA = "chain_update_v1"
MODEL = "claude-sonnet-5-5"
MAX_SEARCHES = 6
RESEARCH_MAX_TOKENS = 16_000
MAX_TOKENS = 16_000
MAX_ATTEMPTS = 2
ADVISED_AFTER_DAYS = 90  # a chain older than this is shown « à mettre à jour »
KINDS = ("search", "reread")
CHANGE_TYPES = ("status", "share", "capacity", "new_producer", "date", "note")
NEW_NODE_TYPES = ("mine", "plant", "recycler", "company", "country")
SHARE_NODE_TYPES = ("country", "mine", "plant", "recycler", "company")
STEPS = {1: "Recherche des changements", 2: "Propositions"}
GOULOT_MARK = " (ajouté par Goulot)"

# Estimates from the reconstruction runs of October 2026 (search results dominate the tokens read).
RESEARCH_TOKENS = {"expected": (150_000, 6_000), "max": (300_000, RESEARCH_MAX_TOKENS)}
STRUCTURE_TOKENS = {"expected": (30_000, 8_000), "max": (60_000, MAX_TOKENS)}


class UpdateError(ValueError):
    """French message: it reaches the user."""


@dataclass(frozen=True)
class UpdateQuote:
    kind: str
    research_usd: float
    research_max_usd: float
    structure_usd: float
    structure_max_usd: float  # per attempt
    searches_max: int

    @property
    def estimate_usd(self) -> float:
        return round(self.research_usd + self.structure_usd, 2)

    @property
    def max_usd(self) -> float:
        return round(self.research_max_usd + MAX_ATTEMPTS * self.structure_max_usd, 2)


def update_quote(kind: str) -> UpdateQuote:
    if kind not in KINDS:
        raise UpdateError("type de mise à jour inconnu")

    def tokens(pair: tuple[int, int]) -> float:
        return cost_usd(MODEL, Usage(input_tokens=pair[0], output_tokens=pair[1]))

    searching = kind == "search"
    searches = MAX_SEARCHES if searching else 0
    search_cost = searches * web_search_price()
    return UpdateQuote(
        kind=kind,
        research_usd=round(tokens(RESEARCH_TOKENS["expected"]) + search_cost, 4) if searching else 0.0,
        research_max_usd=round(tokens(RESEARCH_TOKENS["max"]) + search_cost, 4) if searching else 0.0,
        structure_usd=round(tokens(STRUCTURE_TOKENS["expected"]), 4),
        structure_max_usd=round(tokens(STRUCTURE_TOKENS["max"]), 4),
        searches_max=searches,
    )


def quote_kind(kind: str) -> str:
    """Kind stored with the quote: a quote confirms only its own kind of work."""
    return f"chain_{kind}"


# --- freshness ---------------------------------------------------------------------------------------------------
def _date(text: str | None) -> str | None:
    return str(text)[:10] if text else None


def freshness(conn: sqlite3.Connection, material_id: str, today: datetime | None = None) -> dict[str, Any]:
    """When the chain was researched, last checked against the web, and what waits for the user."""
    now = today or datetime.now(UTC)
    researched = conn.execute("SELECT MIN(created_at) FROM chain_reports WHERE material_id = ?",
                              (material_id,)).fetchone()[0]
    checked = conn.execute("SELECT MAX(created_at) FROM chain_updates WHERE material_id = ? AND kind = 'search'",
                           (material_id,)).fetchone()[0]
    reread = conn.execute("SELECT MAX(created_at) FROM chain_updates WHERE material_id = ? AND kind = 'reread'",
                          (material_id,)).fetchone()[0]
    reference = max(d for d in (researched, checked) if d) if (researched or checked) else None
    age = (now.date() - datetime.fromisoformat(reference).date()).days if reference else None
    pending = conn.execute("SELECT COUNT(*) FROM chain_update_items WHERE material_id = ? AND decision = 'pending'",
                           (material_id,)).fetchone()[0]
    this_month = now.date().isoformat()[:7]
    past = [dict(r) for r in conn.execute(
        "SELECT date, what FROM chain_dates WHERE material_id = ? AND substr(date, 1, 7) < ? ORDER BY date",
        (material_id, this_month))]
    old_shares = conn.execute(
        "SELECT COUNT(*) FROM chain_nodes WHERE material_id = ? AND valid_to IS NULL AND global_share_pct IS NOT NULL "
        "AND share_basis = 'unknown'", (material_id,)).fetchone()[0]
    return {
        "researched_at": _date(researched), "checked_at": _date(checked), "reread_at": _date(reread),
        "age_days": age, "advised": age is not None and age >= ADVISED_AFTER_DAYS, "advised_after_days":
        ADVISED_AFTER_DAYS, "pending": int(pending), "past_dates": past, "shares_without_basis": int(old_shares),
    }


def since(conn: sqlite3.Connection, material_id: str) -> str:
    f = freshness(conn, material_id)
    return f["checked_at"] or f["researched_at"] or ""


# --- prompts -----------------------------------------------------------------------------------------------------
def _share_text(n: dict[str, Any]) -> str:
    if n.get("global_share_pct") is None:
        return "?"
    detail = ", ".join(str(x) for x in (n.get("share_basis") or "unknown", n.get("share_year")) if x)
    return f"{n['global_share_pct']:g} % ({detail})"


def node_lines(nodes: list[dict[str, Any]]) -> str:
    """Public typical chain only: identifier, type, name, country, status, share and capacity."""
    lines = []
    for n in nodes:
        name = str(n["name"]) + (GOULOT_MARK if n.get("added_by") == "goulot" else "")
        cells = (n["node_key"], n["node_type"], name, n["country_iso2"], n.get("status") or "unknown",
                 _share_text(n), n.get("capacity_text") or "unknown")
        lines.append(" | ".join(escape(str(c)) for c in cells))
    return "\n".join(lines)


def _system(prompt: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": load_prompt(prompt), "cache_control": {"type": "ephemeral", "ttl": "1h"}}]


def research_params(material: dict[str, Any], nodes: list[dict[str, Any]], since_date: str,
                    today: str) -> dict[str, Any]:
    user = "\n\n".join([material_block(material), tag("since", escape(since_date)), tag("today", today),
                        tag("chain_nodes", node_lines(nodes))])
    return {
        "model": MODEL, "max_tokens": RESEARCH_MAX_TOKENS, "system": _system(RESEARCH_PROMPT),
        "tools": [{"type": research.WEB_SEARCH_TOOL, "name": "web_search", "max_uses": MAX_SEARCHES,
                   "allowed_callers": ["direct"]}],  # direct: otherwise no citation (decision of 2026-10-16)
        "messages": [{"role": "user", "content": user}],
    }


def structure_params(material: dict[str, Any], nodes: list[dict[str, Any]], report: str,
                     sources: list[dict[str, str]], today: str) -> dict[str, Any]:
    user = "\n\n".join([material_block(material), tag("today", today), tag("chain_nodes", node_lines(nodes)),
                        tag("research_report", escape(report)), sources_block(sources)])
    return {
        "model": MODEL, "max_tokens": MAX_TOKENS, "system": _system(PROMPT), "thinking": {"type": "adaptive"},
        "output_config": {"effort": "medium", "format": {"type": "json_schema", "schema": schema(SCHEMA)}},
        "messages": [{"role": "user", "content": user}],
    }


def renumber(report: str, sources: list[dict[str, str]], first: int) -> tuple[str, list[dict[str, str]]]:
    """New sources follow the stored ones (src56, src57…): `sources` keeps one ref per material."""
    mapping = {s["ref"]: f"src{first + i}" for i, s in enumerate(sources)}
    text = re.sub(r"\[(src\d+)\]", lambda m: f"[{mapping.get(m.group(1), m.group(1))}]", report)
    return text, [{**s, "ref": mapping[s["ref"]]} for s in sources]


# --- code checks on the proposals --------------------------------------------------------------------------------
def _slug(text: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:60] or "producteur"


def check_changes(output: dict[str, Any], nodes: list[dict[str, Any]], allowed: set[str], today: str
                  ) -> tuple[list[dict[str, Any]], list[str]]:
    """Proposals kept (each with only the fields its type sets) and French notes on those dropped."""
    by_key = {n["node_key"]: n for n in nodes}
    taken = set(by_key)
    kept: list[dict[str, Any]] = []
    notes: list[str] = []
    year_max = int(today[:4]) + 1
    for c in output["changes"]:
        kind = str(c["change_type"]).strip().lower()
        summary = " ".join(str(c["summary"]).split())[:400]
        refs = [r for r in c["source_ids"] if r in allowed]
        confidence = c["confidence"] if c["confidence"] in ("high", "medium", "low") else "low"
        anomalies: list[str] = []
        if set(c["source_ids"]) - allowed:
            anomalies.append("source inconnue citée, retirée")
        if not refs:
            notes.append(f"Proposition sans source écartée : {summary[:120]}")
            continue
        node = by_key.get(c["node_id"])
        payload: dict[str, Any]
        if kind in ("status", "share", "capacity"):
            if node is None or node.get("added_by") == "goulot":
                notes.append(f"Proposition sur un maillon inconnu écartée : {summary[:120]}")
                continue
            if kind == "status":
                if c["status"] not in ("operating", "idled", "closed", "project") or c["status"] == node.get("status"):
                    notes.append(f"Changement d'état sans objet écarté : {summary[:120]}")
                    continue
                payload = {"status": c["status"]}
            elif kind == "share":
                share, basis, year = c["share_pct"], c["share_basis"], c["share_year"]
                if (share is None or not 0 <= share <= 100 or basis not in ("production", "capacity", "exports")
                        or node["node_type"] not in SHARE_NODE_TYPES):
                    notes.append(f"Part mondiale invalide écartée : {summary[:120]}")
                    continue
                if year is not None and not 1990 <= int(year) <= year_max:
                    year = None
                    anomalies.append("année de la part invraisemblable, retirée")
                payload = {"global_share_pct": round(float(share), 2), "share_basis": basis,
                           "share_year": None if year is None else int(year)}
            else:
                text = " ".join(str(c["capacity_text"]).split())[:200]
                if not text or text.lower() == "unknown":
                    notes.append(f"Capacité vide écartée : {summary[:120]}")
                    continue
                payload = {"capacity_text": text}
            node_key = node["node_key"]
        elif kind == "new_producer":
            feeds = by_key.get(c["feeds_node_id"])
            name = " ".join(str(c["new_name"]).split())[:200]
            iso = str(c["new_country_iso2"]).strip().upper()
            iso = iso if ISO2.match(iso) else "unknown"
            if feeds is None or feeds["node_type"] != "material" or c["new_node_type"] not in NEW_NODE_TYPES \
                    or not name:
                notes.append(f"Nouveau producteur incomplet écarté : {summary[:120]}")
                continue
            if c["new_node_type"] == "country" and iso == "unknown":
                notes.append(f"Pays producteur sans code pays écarté : {summary[:120]}")
                continue
            if any(n["name"].casefold() == name.casefold() for n in nodes):
                notes.append(f"Producteur déjà dans la chaîne écarté : {name[:80]}")
                continue
            base = c["node_id"] if NODE_ID.match(str(c["node_id"])) else _slug(name)
            node_key, i = base, 2
            while node_key in taken:
                node_key, i = f"{base}_{i}", i + 1
            taken.add(node_key)
            status = c["status"] if c["status"] in ("operating", "idled", "closed", "project") else "unknown"
            if status == "closed":  # a closed site supplies nothing: not worth a node (seen on 2026-11-13)
                notes.append(f"Site déjà fermé, non ajouté : {name[:80]}")
                taken.discard(node_key)
                continue
            capacity = " ".join(str(c["capacity_text"]).split())[:200] or "unknown"
            place = " ".join(str(c["new_place"]).split())[:200] or "unknown"
            payload = {"node_type": c["new_node_type"], "name": name,
                       "product": " ".join(str(c["new_product"]).split())[:200] or feeds["name"],
                       "country_iso2": iso, "place": place, "status": status, "capacity_text": capacity,
                       "feeds": feeds["node_key"]}
        elif kind == "date":
            when = str(c["date"]).strip()
            what = " ".join(str(c["date_what"]).split())[:300]
            if not DATE.match(when) or len(when) < 7 or when[:7] < today[:7] or not what:
                notes.append(f"Échéance passée ou mal datée écartée : {summary[:120]}")
                continue
            payload, node_key = {"date": when, "what": what}, ""
        elif kind == "note":
            payload, node_key = {}, ""
        else:
            notes.append(f"Type de proposition inconnu écarté : {summary[:120]}")
            continue
        kept.append({"change_type": kind, "node_key": node_key, "summary": summary or kind, "payload": payload,
                     "confidence": confidence, "source_refs": refs, "anomalies": anomalies})
    return kept, notes


# --- the job -----------------------------------------------------------------------------------------------------
def start_job(db: Database, material_id: str) -> str:
    """Refused while a reconstruction or another update of the material runs."""
    job_id = str(uuid.uuid4())
    with db.transaction() as conn:
        busy = conn.execute("SELECT 1 FROM jobs WHERE material_id = ? AND state = 'running' AND kind IN "
                            "('reconstruction', 'chain_update') LIMIT 1", (material_id,)).fetchone()
        if busy:
            raise UpdateError("un travail est déjà en cours sur cette chaîne : attendez sa fin")
        conn.execute("INSERT INTO jobs (id, material_id, kind, step, state, started_at) "
                     "VALUES (?, ?, 'chain_update', 1, 'running', ?)", (job_id, material_id, utcnow()))
    return job_id


def _finish(db: Database, job_id: str, material_id: str, *, ok: bool, message: str) -> float:
    with db.transaction() as conn:
        started = conn.execute("SELECT started_at FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
        cost = float(conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM llm_calls WHERE material_id = ? AND at >= ?",
                                  (material_id, started)).fetchone()[0])
        conn.execute("UPDATE jobs SET state = ?, message = ?, cost_usd = ?, finished_at = ? WHERE id = ?",
                     ("done" if ok else "failed", message, round(cost, 6), utcnow(), job_id))
    return cost


def _material_for_prompt(material: dict[str, Any]) -> dict[str, Any]:
    from goulot.chain.service import material_for_prompt

    return material_for_prompt(material)


def run_update(db: Database, budget: Budget, backend: MessageBackend, material_id: str, job_id: str, kind: str, *,
               budget_name: str = "reconstruction", today: str | None = None) -> float:
    """Search (optional), then proposals. Returns the cost in dollars; failures end the job with a French message."""
    day = today or research.today_iso()
    quote = update_quote(kind)
    with db.connect() as conn:
        material = store.get_material(conn, material_id)
        if material is None:
            raise UpdateError("matière inconnue")
        chain = store.load_chain(conn, material_id)
        stored = conn.execute("SELECT report FROM chain_reports WHERE material_id = ? ORDER BY id DESC LIMIT 1",
                              (material_id,)).fetchone()
        start = since(conn, material_id)
    nodes = chain["nodes"]
    prompt_material = _material_for_prompt(material)
    cas = material["cas"]
    try:
        if kind == "search":
            spec = CallSpec(key=f"chain_update_research_{cas}", purpose="chain_update_research", budget=budget_name,
                            max_cost_usd=quote.research_max_usd, material_id=material_id)
            result = call(backend, budget, spec, research_params(prompt_material, nodes, start or "inconnue", day))
            report, sources = research.build_report(result.content)
            if not sources:
                raise LLMError("La recherche n'a renvoyé aucune source citée : aucune proposition.")
            last = max((int(s["ref"][3:]) for s in chain["sources"] if s["ref"][3:].isdigit()), default=0)
            report, sources = renumber(report, sources, last + 1)
        else:
            if stored is None or not stored[0]:
                raise UpdateError("aucun rapport de recherche enregistré pour cette chaîne")
            report = stored[0]
            sources = [{k: s[k] for k in ("ref", "url", "title", "publisher", "page_age")} for s in chain["sources"]]
        with db.transaction() as conn:
            conn.execute("UPDATE jobs SET step = 2 WHERE id = ?", (job_id,))
        output: dict[str, Any] | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            key = f"chain_update_{kind}_{cas}" + ("" if attempt == 1 else f"_retry{attempt - 1}")
            spec = CallSpec(key=key, purpose="chain_update", budget=budget_name,
                            max_cost_usd=quote.structure_max_usd, material_id=material_id)
            result = call(backend, budget, spec, structure_params(prompt_material, nodes, report, sources, day))
            try:
                output = validate_schema(SCHEMA, parse_json_output(result.content))
                break
            except SchemaError:
                continue
        if output is None:
            raise LLMError("La sortie de l'IA ne respecte pas le schéma, même après un nouvel essai.")
        items, dropped = check_changes(output, nodes, {s["ref"] for s in sources}, day)
        notes = [" ".join(str(q).split())[:400] for q in output["open_questions"]] + dropped
        with db.transaction() as conn:
            save_update(conn, material_id, kind, start, report if kind == "search" else "", sources if
                        kind == "search" else [], items, notes, bool(output["injection_suspected"]))
    except (LLMError, BudgetExceeded, CircuitOpen, UpdateError) as exc:
        return _finish(db, job_id, material_id, ok=False, message=str(exc))
    except Exception:
        log.exception("chain update failed")
        _finish(db, job_id, material_id, ok=False, message="Erreur interne pendant la mise à jour.")
        raise
    message = (f"{len(items)} proposition(s) à relire." if items else "Aucun changement proposé.")
    if output["injection_suspected"]:
        message += " Contenu piégé signalé : relisez les sources."
    return _finish(db, job_id, material_id, ok=True, message=message)


def save_update(conn: sqlite3.Connection, material_id: str, kind: str, since_date: str, report: str,
                sources: list[dict[str, str]], items: list[dict[str, Any]], notes: list[str],
                injection: bool) -> int:
    now = utcnow()
    for s in sources:
        conn.execute("INSERT OR IGNORE INTO sources (material_id, ref, url, title, publisher, page_age, retrieved_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", (material_id, s["ref"], s["url"], s.get("title", ""),
                                                      s.get("publisher", ""), s.get("page_age", ""), now))
    # Proposals still pending from an earlier update are superseded: only the latest reading is relevant.
    conn.execute("UPDATE chain_update_items SET decision = 'rejected', decided_at = ? WHERE material_id = ? "
                 "AND decision = 'pending'", (now, material_id))
    cur = conn.execute("INSERT INTO chain_updates (material_id, kind, since, report, notes, injection_suspected, "
                       "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (material_id, kind, since_date or "", report, json.dumps(notes, ensure_ascii=False),
                        int(injection), now))
    update_id = int(cur.lastrowid or 0)
    for it in items:
        conn.execute(
            "INSERT INTO chain_update_items (update_id, material_id, change_type, node_key, summary, payload, "
            "confidence, source_refs, anomalies) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (update_id, material_id, it["change_type"], it["node_key"], it["summary"],
             json.dumps(it["payload"], ensure_ascii=False, sort_keys=True), it["confidence"],
             json.dumps(it["source_refs"]), json.dumps(it["anomalies"], ensure_ascii=False)))
    return update_id


# --- what the screen shows, and the user's decisions -------------------------------------------------------------
def overview(conn: sqlite3.Connection, material_id: str) -> dict[str, Any]:
    latest = conn.execute("SELECT * FROM chain_updates WHERE material_id = ? ORDER BY id DESC LIMIT 1",
                          (material_id,)).fetchone()
    items = []
    for r in conn.execute("SELECT * FROM chain_update_items WHERE material_id = ? AND (decision = 'pending' OR "
                          "update_id = ?) ORDER BY id", (material_id, latest["id"] if latest else -1)):
        item = dict(r)
        for key in ("payload", "source_refs", "anomalies"):
            item[key] = json.loads(item[key])
        items.append(item)
    last = None
    if latest:
        last = {"kind": latest["kind"], "created_at": _date(latest["created_at"]), "since": latest["since"],
                "notes": json.loads(latest["notes"]), "injection_suspected": bool(latest["injection_suspected"])}
    return {"freshness": freshness(conn, material_id), "last_update": last, "items": items}


def decide(conn: sqlite3.Connection, material_id: str, item_id: int, accept: bool) -> None:
    row = conn.execute("SELECT * FROM chain_update_items WHERE id = ? AND material_id = ?",
                       (item_id, material_id)).fetchone()
    if row is None:
        raise UpdateError("proposition introuvable")
    if row["decision"] != "pending":
        raise UpdateError("proposition déjà traitée")
    if accept:
        _apply(conn, material_id, dict(row))
    conn.execute("UPDATE chain_update_items SET decision = ?, decided_at = ? WHERE id = ?",
                 ("accepted" if accept else "rejected", utcnow(), item_id))
    if accept:
        store.refresh_rules(conn, material_id)


def _source_ids(conn: sqlite3.Connection, material_id: str, refs: list[str]) -> list[int]:
    return [r[0] for ref in refs for r in conn.execute(
        "SELECT id FROM sources WHERE material_id = ? AND ref = ?", (material_id, ref)).fetchall()]


def _apply(conn: sqlite3.Connection, material_id: str, item: dict[str, Any]) -> None:
    from goulot.chain.places import computed_position

    payload = json.loads(item["payload"])
    refs = json.loads(item["source_refs"])
    kind = item["change_type"]
    now = utcnow()
    if kind in ("status", "share", "capacity"):
        row = conn.execute("SELECT * FROM chain_nodes WHERE material_id = ? AND node_key = ? AND valid_to IS NULL",
                           (material_id, item["node_key"])).fetchone()
        if row is None:
            raise UpdateError("ce maillon n'existe plus : proposition sans objet")
        changes = dict(payload)
        if kind == "share":
            changes["confidence"] = item["confidence"]
        store.revise_node(conn, row, changes, by_user=True)
        new_id = conn.execute("SELECT id FROM chain_nodes WHERE material_id = ? AND node_key = ? AND valid_to IS NULL",
                              (material_id, item["node_key"])).fetchone()[0]
        for sid in _source_ids(conn, material_id, refs):
            conn.execute("INSERT OR IGNORE INTO node_sources VALUES (?, ?)", (new_id, sid))
    elif kind == "new_producer":
        feeds = conn.execute("SELECT * FROM chain_nodes WHERE material_id = ? AND node_key = ? AND valid_to IS NULL",
                             (material_id, payload["feeds"])).fetchone()
        if feeds is None:
            raise UpdateError("le produit de ce producteur n'est plus dans la chaîne : proposition sans objet")
        if conn.execute("SELECT 1 FROM chain_nodes WHERE material_id = ? AND node_key = ? AND valid_to IS NULL",
                        (material_id, item["node_key"])).fetchone():
            raise UpdateError("un maillon porte déjà cet identifiant")
        tier = min(3, int(feeds["tier"]) + 1)
        node = {"node_type": payload["node_type"], "name": payload["name"], "place": payload["place"],
                "country_iso2": payload["country_iso2"]}
        lat, lon, coord_source = computed_position(node)
        row = {
            "material_id": material_id, "node_key": item["node_key"], "node_type": payload["node_type"],
            "name": payload["name"], "product": payload["product"], "tier": tier,
            "country_iso2": payload["country_iso2"], "place": payload["place"], "lat": lat, "lon": lon,
            "global_share_pct": None, "buffer_days": float(DEFAULT_BUFFER_DAYS.get(payload["node_type"], 0)),
            "buffer_is_default": 1, "capacity_text": payload["capacity_text"], "confidence": item["confidence"],
            "notes": item["summary"], "anomalies": "[]", "user_modified": 1, "valid_from": now, "valid_to": None,
            "recorded_at": now, "coord_source": coord_source, "added_by": "", "status": payload["status"],
            "share_basis": "unknown", "share_year": None,
        }
        cur = conn.execute(store.INSERT_NODE, [row[c] for c in store.NODE_COLUMNS])
        for sid in _source_ids(conn, material_id, refs):
            conn.execute("INSERT OR IGNORE INTO node_sources VALUES (?, ?)", (cur.lastrowid, sid))
        to_sink = int(feeds["tier"]) == 0
        mode = "unknown" if to_sink else "none"  # a producer of the purchased material still has to deliver it
        edge = {
            "material_id": material_id, "from_key": item["node_key"], "to_key": feeds["node_key"],
            "relation": "produces", "transport_mode": mode, "lead_time_days": float(DEFAULT_LEAD_DAYS[mode]),
            "lead_time_basis": "default", "share_pct": None, "confidence": item["confidence"], "anomalies": "[]",
            "user_modified": 1, "valid_from": now, "valid_to": None, "recorded_at": now,
        }
        conn.execute(store.INSERT_EDGE, [edge[c] for c in store.EDGE_COLUMNS])
    elif kind == "date":
        conn.execute("INSERT INTO chain_dates (material_id, date, what, node_keys, source_refs, recorded_at) "
                     "VALUES (?, ?, ?, '[]', ?, ?)", (material_id, payload["date"], payload["what"],
                                                      json.dumps(refs), now))
    elif kind == "note":
        conn.execute("INSERT INTO chain_risks (material_id, kind, risk_type, description, source_refs, recorded_at) "
                     "VALUES (?, 'risk', 'other', ?, ?, ?)", (material_id, item["summary"], json.dumps(refs), now))


def job_steps() -> dict[int, str]:
    return dict(STEPS)


def quote_payload(quote: UpdateQuote) -> dict[str, Any]:
    return {**asdict(quote), "estimate_usd": quote.estimate_usd, "max_usd": quote.max_usd}
