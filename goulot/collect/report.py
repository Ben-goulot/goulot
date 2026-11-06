"""Watch diagnostic (docs/DECISIONS.md, 27 October 2026): why nothing is kept. No AI, no private data.

`history`: what the recorded runs say, per source and per material, over the last days. Runs recorded before
migration 0013 have no `matched` count: rejected items and duplicates cannot be told apart for them.
`live`: one dry collection with the public rules, writing nothing, that tells rejected items from duplicates now.
Only public tables are read (rules, chain nodes, runs, articles); Ma chaîne is never opened.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import httpx2

from goulot.collect import sources
from goulot.collect.base import Item, enabled, iso, load_sources
from goulot.collect.rules import active_rules, match, title_only, too_old
from goulot.collect.runner import MAX_ITEMS_PER_SOURCE, SecretGetter, _fetchers

EXAMPLES = 3
_PER_SOURCE = ("SELECT source, COUNT(*) AS runs, SUM(ok = 0) AS failed, SUM(fetched) AS fetched, "
               "SUM(matched) AS matched, SUM(kept) AS kept, SUM(title_only) AS title_only, "
               "MAX(CASE WHEN kept > 0 THEN started_at END) AS last_kept "
               "FROM collect_runs WHERE started_at >= ? GROUP BY source ORDER BY source")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM pragma_table_info(?)", (table,))}


def history(conn: sqlite3.Connection, now: datetime, days: int = 14) -> dict[str, Any]:
    since = iso(now - timedelta(days=days))
    has_matched = "matched" in _columns(conn, "collect_runs")
    per_source = []
    query = _PER_SOURCE if has_matched else _PER_SOURCE.replace("SUM(matched)", "NULL")
    if "title_only" not in _columns(conn, "collect_runs"):
        query = query.replace("SUM(title_only)", "0")
    for r in conn.execute(query, (since,)):
        errors = [(e["error"], e["n"]) for e in conn.execute(
            "SELECT error, COUNT(*) AS n FROM collect_runs WHERE source = ? AND started_at >= ? AND ok = 0 "
            "GROUP BY error ORDER BY n DESC LIMIT 3", (r["source"], since))]
        per_source.append({**dict(r), "errors": errors})
    cycles = dict(conn.execute(
        "SELECT COUNT(*) AS cycles, SUM(ok = 1) AS ok, SUM(finished_at IS NULL) AS unfinished, "
        "MAX(started_at) AS last FROM cycles WHERE started_at >= ?", (since,)).fetchone())
    materials = []
    for m in conn.execute("SELECT id, name, cas, status, validated_at FROM materials WHERE status != 'archived' "
                          "ORDER BY name"):
        rules = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM watch_rules WHERE material_id = ? AND active = 1 GROUP BY kind", (m["id"],)
        ).fetchall())
        history_ = dict(conn.execute(
            "SELECT COUNT(*) AS total, SUM(active = 0) AS inactive, MIN(created_at) AS first, "
            "MAX(created_at) AS last FROM watch_rules WHERE material_id = ?", (m["id"],)).fetchone())
        nodes = conn.execute("SELECT COUNT(*) FROM chain_nodes WHERE material_id = ? AND valid_to IS NULL",
                             (m["id"],)).fetchone()[0]
        # Articles that matched a public rule of this material (a rule of Ma chaîne leaves no id in rules_hit).
        statuses = dict(conn.execute(
            "SELECT a.status, COUNT(DISTINCT a.id) FROM articles a, json_each(a.rules_hit) j "
            "JOIN watch_rules w ON w.id = j.value WHERE w.material_id = ? AND a.first_seen_at >= ? "
            "GROUP BY a.status", (m["id"], since)).fetchall())
        materials.append({"name": m["name"], "cas": m["cas"], "status": m["status"], "rules": rules,
                          "articles": statuses, "validated_at": m["validated_at"], "rule_history": history_,
                          "nodes": nodes})
    base = {
        "schema": conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if "version" in _columns(conn, "schema_migrations") else None,
        "rules": dict(conn.execute("SELECT COUNT(*) AS total, SUM(active = 1) AS active, "
                                   "SUM(material_id NOT IN (SELECT id FROM materials)) AS orphans "
                                   "FROM watch_rules").fetchone()),
    }
    # What the triage made of each source's articles, and what the watch cost: to keep a feed or drop it.
    outcomes: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT source, status, COUNT(*) AS n FROM articles WHERE first_seen_at >= ? "
                          "AND kind IN ('news', 'policy') GROUP BY source, status", (since,)):
        outcomes.setdefault(r["source"], {})[r["status"]] = r["n"]
    copies = conn.execute("SELECT COUNT(*) FROM articles WHERE first_seen_at >= ? "
                          "AND json_extract(details, '$.copy_of') IS NOT NULL", (since,)).fetchone()[0]
    costs = [dict(r) for r in conn.execute(
        "SELECT purpose, COUNT(*) AS calls, SUM(cost_usd) AS cost FROM llm_calls WHERE budget = 'watch' AND at >= ? "
        "GROUP BY purpose ORDER BY purpose", (since,))]
    return {"days": days, "has_matched": has_matched, "cycles": cycles, "sources": per_source,
            "materials": materials, "base": base, "outcomes": outcomes, "copies": copies, "costs": costs,
            "impact": _impact(conn, since), "verdicts": _verdicts(conn, since)}


def _trigger(conn: sqlite3.Connection, event_id: str) -> str:
    """Why an event met the analysis trigger, read from the nodes it touches (the first reason that applies)."""
    rows = conn.execute(
        "SELECT n.node_type, n.global_share_pct FROM event_links l JOIN chain_nodes n ON n.material_id = l.material_id "
        "AND n.node_key = l.node_key AND n.valid_to IS NULL WHERE l.event_id = ?", (event_id,)).fetchall()
    if not rows:
        return "sans maillon"
    if any(r["node_type"] in ("chokepoint", "waterway") for r in rows):
        return "détroit ou voie d'eau"
    if any(r["global_share_pct"] is not None and r["global_share_pct"] >= 0.5 for r in rows):
        return "part connue ≥ 0,5 %"
    if any(r["global_share_pct"] is None for r in rows):
        return "part inconnue"
    return "part < 0,5 %"


def _impact(conn: sqlite3.Connection, since: str | None) -> list[dict[str, Any]]:
    """Where the impact analyses' money went: by origin and type of event, trigger, severity hint, and what the
    analysis concluded (severity 0 means it found no material impact). Read-only, no AI."""
    groups: dict[tuple[str, str, str, int, str], dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT a.event_id, a.state, a.cost_usd, a.result, e.origin, e.event_type, e.severity_hint, "
        "m.name AS material FROM analyses a JOIN events e ON e.id = a.event_id "
        "JOIN materials m ON m.id = a.material_id WHERE a.created_at >= ?", (since or "",)).fetchall()
    for r in rows:
        result = json.loads(r["result"]) if r["result"] else {}
        top = max((c.get("severity", 0) for c in result.get("commodity_impacts", [])), default=None)
        outcome = r["state"] if r["state"] != "done" else ("impact nul" if not top else f"sévérité {top}")
        key = (r["origin"], r["event_type"], _trigger(conn, r["event_id"]), r["severity_hint"], outcome)
        g = groups.setdefault(key, {"analyses": 0, "events": set(), "cost": 0.0, "materials": set()})
        g["analyses"] += 1
        g["events"].add(r["event_id"])
        g["cost"] += r["cost_usd"] or 0.0
        g["materials"].add(r["material"])
    out = [{"origin": k[0], "type": k[1], "trigger": k[2], "severity_hint": k[3], "outcome": k[4],
            "analyses": g["analyses"], "events": len(g["events"]), "cost": round(g["cost"], 4),
            "materials": len(g["materials"])} for k, g in groups.items()]
    return sorted(out, key=lambda x: -x["cost"])


def _verdicts(conn: sqlite3.Connection, since: str | None) -> list[dict[str, Any]]:
    """Market verdict of each material and the public events behind it, to tell why it says what it says."""
    from goulot.pipeline.visibility import set_aside

    out = []
    for m in conn.execute("SELECT id, name FROM materials WHERE status = 'validated' ORDER BY name"):
        row = conn.execute("SELECT verdict, event_ids FROM verdicts WHERE material_id = ? AND scope = 'market' "
                           "AND current = 1", (m["id"],)).fetchone()
        retained = []
        for event_id in json.loads(row["event_ids"]) if row else []:
            e = conn.execute("SELECT id, title, origin, confirmation, severity_hint, lineages FROM public_events "
                             "WHERE id = ?", (event_id,)).fetchone()
            retained.append({**dict(e), "set_aside": set_aside(conn, event_id)} if e else
                            {"id": event_id, "title": "(introuvable)", "origin": "", "confirmation": "",
                             "severity_hint": None, "lineages": 0, "set_aside": True})
        origins = dict(conn.execute("SELECT origin, COUNT(*) FROM public_events WHERE material_id = ? "
                                    "AND first_seen_at >= ? GROUP BY origin", (m["id"], since)).fetchall())
        out.append({"name": m["name"], "verdict": row["verdict"] if row else None, "retained": retained,
                    "origins": origins})
    return out


def _printable(text: str) -> str:
    """External text shown in a terminal: control and escape characters removed."""
    return "".join(ch for ch in text if ch.isprintable())[:120]


def live(conn: sqlite3.Connection, http: httpx2.Client, secret: SecretGetter, now: datetime,
         lookback: timedelta = timedelta(hours=24),
         fetchers: dict[str, Callable[[httpx2.Client, datetime], list[Item]]] | None = None) -> list[dict[str, Any]]:
    """One collection with the public rules, nothing written: per source, rejected items and duplicates."""
    rules = active_rules(conn)
    country_nodes = {r[0]: r[0] for r in conn.execute(
        "SELECT node_key FROM chain_nodes WHERE valid_to IS NULL AND node_type = 'country'")}
    out = []
    for source_id, fetch in (fetchers or _fetchers(conn, now, secret)).items():
        if not enabled(source_id):
            continue
        row: dict[str, Any] = {"source": source_id, "family": load_sources()[source_id].get("family", ""),
                               "fetched": 0, "rejected": 0, "duplicates": 0, "new": 0, "error": "", "examples": []}
        try:
            try:
                items = fetch(http, now - lookback)[:MAX_ITEMS_PER_SOURCE]
            except sources.PartialFailure as exc:
                items, row["error"] = exc.items, f"partiel : {exc}"
        except (httpx2.HTTPError, sources.SourceError, ValueError) as exc:
            row["error"] = type(exc).__name__ + (f": {exc}" if isinstance(exc, sources.SourceError) else "")
            out.append(row)
            continue
        row["fetched"] = len(items)
        for item in items:
            if title_only(item) or too_old(item, now) or not match(item, rules, country_nodes):
                row["rejected"] += 1
                if item.kind == "news" and len(row["examples"]) < EXAMPLES:
                    row["examples"].append(_printable(item.title))
            elif conn.execute("SELECT 1 FROM articles WHERE url_hash = ?", (item.identity(),)).fetchone():
                row["duplicates"] += 1
            else:
                row["new"] += 1
        out.append(row)
    return out


def render(report: dict[str, Any], sample: list[dict[str, Any]] | None) -> str:
    c = report["cycles"]
    lines = [f"Veille des {report['days']} derniers jours : {c['cycles'] or 0} cycles, {c['ok'] or 0} réussis, "
             f"{c['unfinished'] or 0} interrompus ; dernier : {c['last'] or 'aucun'}.", "",
             "Par source (lus / titres seuls écartés / filtrés / nouveaux gardés ; échecs) :"]
    for s in report["sources"]:
        matched = "?" if s["matched"] is None else s["matched"]
        line = (f"  {s['source']:<28} {s['fetched'] or 0:>6} lus  {s['title_only'] or 0:>5} titres seuls  "
                f"{matched:>5} filtrés  {s['kept'] or 0:>5} gardés  {s['failed'] or 0}/{s['runs']} échecs")
        if s["errors"]:
            line += "  [" + ", ".join(f"{e} ×{n}" for e, n in s["errors"]) + "]"
        lines.append(line + f"  dernier gardé : {s['last_kept'] or 'jamais'}")
    if not report["has_matched"]:
        lines.append("  (« ? » : cette version ne comptait pas encore les éléments retenus par le filtre.)")
    lines += ["", "Par matière (règles actives ; articles gardés par statut) :"]
    for m in report["materials"]:
        rules = ", ".join(f"{k} {n}" for k, n in sorted(m["rules"].items())) or "aucune"
        arts = ", ".join(f"{k} {n}" for k, n in sorted(m["articles"].items())) or "aucun"
        lines.append(f"  {m['name']} ({m['cas']}, {m['status']}) — règles : {rules} — articles : {arts}")
        h = m["rule_history"]
        lines.append(f"      validée le {m['validated_at'] or '—'} ; {m['nodes']} maillons ; règles en base : "
                     f"{h['total'] or 0} dont {h['inactive'] or 0} inactives, créées du {h['first'] or '—'} "
                     f"au {h['last'] or '—'}")
    b = report["base"]
    lines.append(f"  Base : migration {b['schema']} ; règles {b['rules']['total'] or 0}, actives "
                 f"{b['rules']['active'] or 0}, sans matière {b['rules']['orphans'] or 0}.")
    if report.get("outcomes"):
        lines += ["", "Tri des articles gardés, par source (pertinents = regroupés en événement) :"]
        for source, by_status in sorted(report["outcomes"].items()):
            judged = sum(n for k, n in by_status.items() if k != "new")
            useful = by_status.get("grouped", 0) + by_status.get("triaged", 0)
            share = f"{100 * useful / judged:.0f} %" if judged else "—"
            detail = ", ".join(f"{k} {n}" for k, n in sorted(by_status.items()))
            lines.append(f"  {source:<28} {share:>5} pertinents  ({detail})")
        lines.append(f"  Copies jugées sans appel IA (même histoire déjà triée) : {report.get('copies', 0)}")
    if report.get("costs") is not None:
        total = sum(c["cost"] or 0 for c in report["costs"])
        days = report["days"]
        lines += ["", f"Coût IA de la veille : {total:.2f} $ sur {days} jours, soit environ "
                      f"{total * 30 / days:.2f} $ par mois :"]
        lines += [f"  {c['purpose']:<28} {c['calls']:>5} appels  {c['cost'] or 0:.3f} $" for c in report["costs"]]
    if report.get("impact"):
        total = sum(i["cost"] for i in report["impact"])
        lines += ["", f"Analyses d'impact : {total:.2f} $ — par origine, type, déclencheur, sévérité préliminaire et "
                      "conclusion (analyses / événements / coût) :"]
        for i in report["impact"][:25]:
            lines.append(f"  {i['origin']:<9} {i['type'][:26]:<26} {i['trigger']:<22} s{i['severity_hint']} "
                         f"→ {i['outcome']:<12} {i['analyses']:>3} / {i['events']:>3}  {i['cost']:.3f} $")
    if report.get("verdicts"):
        lines += ["", "Verdict de chaque matière et événements qui le justifient :"]
        for v in report["verdicts"]:
            origins = ", ".join(f"{k} {n}" for k, n in sorted(v["origins"].items())) or "aucun"
            lines.append(f"  {v['name']} : {v['verdict'] or 'pas de verdict'} — événements de la période : {origins}")
            for e in v["retained"]:
                aside = " ; ÉCARTÉ depuis" if e["set_aside"] else ""
                lines.append(f"      {e['id']} : {_printable(e['title'])} ({e['origin']}, {e['confirmation']}, "
                             f"gravité {e['severity_hint']}, {e['lineages']} source(s){aside})")
    if sample is not None:
        lines += ["", "Essai à blanc sur 24 h (règles publiques, rien n'est enregistré) :"]
        for r in sample:
            line = (f"  {r['source']:<28} {r['fetched']:>5} lus  {r['rejected']:>5} rejetés  "
                    f"{r['duplicates']:>5} déjà en base  {r['new']:>5} nouveaux")
            lines.append(line + (f"  [{r['error']}]" if r["error"] else ""))
            lines += [f"      rejeté : {t}" for t in r["examples"]]
    return "\n".join(lines)
